import contextlib
import io
import os
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from types import SimpleNamespace

# ----- FLASK APP ------
import click
from flask import Flask, abort, flash, redirect, render_template, request, url_for
from flask_login import LoginManager, UserMixin, current_user, login_required, login_user, logout_user
from flask_wtf import CSRFProtect, FlaskForm
from flask_wtf.file import FileAllowed, FileField, FileRequired
from werkzeug.security import check_password_hash, generate_password_hash
from wtforms import (BooleanField, DecimalField, EmailField, IntegerField, PasswordField, SelectField, StringField, TextAreaField)
from wtforms.validators import (DataRequired, EqualTo, InputRequired, Length, NumberRange, Optional, Regexp, ValidationError)

# ----- DATABASE -----
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import CheckConstraint, Computed, Identity, MetaData, func, select

app = Flask(__name__)

# ------ APP CONFIG ------
# In production the key comes from the environment; the fallback is for local use only.
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-only-change-me")
CSRFProtect(app)

db_url = os.environ.get("DATABASE_URL", "postgresql+psycopg:///chemotriage")
# Providers give postgres:// or postgresql://; psycopg 3 needs the +psycopg driver name.
db_url = db_url.replace("postgres://", "postgresql+psycopg://", 1).replace("postgresql://", "postgresql+psycopg://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
# ------ END ------

# ------ DB CONFIG ------
# Predictable constraint names, so later migrations can find them.
db = SQLAlchemy(app, metadata=MetaData(naming_convention={
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}))
migrate = Migrate(app, db)

# ------ LOGIN CONFIG ------
login_manager = LoginManager(app)
# ------ Where @login_required sends visitors who are not signed in ------
login_manager.login_view = "home"  
login_manager.login_message = "Sign in to continue."
login_manager.login_message_category = "info"

MAX_FAILED_ATTEMPTS = 5
LOCK_MINUTES = 15
# ------ END ------

# Heavy atoms env var
MAX_HEAVY_ATOMS = 200

# ------ TABLES ------
# CHECK constraints go in __table_args__ and need a name: Flask-Migrate skips
# the ones written inside a column.

# ------ 1. ROLE ------
class Role(db.Model):
    __tablename__ = "role"

    id = db.Column(db.Integer, Identity(always=True), primary_key=True)
    name = db.Column(db.String(30), nullable=False, unique=True)
# ------ END ROLE TABLE ------

# ------ 2. APP USER ------
class AppUser(UserMixin, db.Model):
    __tablename__ = "app_user"  # "user" is a reserved word in PostgreSQL

    id = db.Column(db.Integer, Identity(always=True), primary_key=True)
    role_id = db.Column(db.Integer, db.ForeignKey("role.id"), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), nullable=False, unique=True)
    password_hash = db.Column(db.String(255), nullable=False)
    institution = db.Column(db.String(150))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=func.now())
    is_active = db.Column(db.Boolean, nullable=False, server_default="true")
    failed_attempts = db.Column(db.SmallInteger, nullable=False, server_default="0")  # consecutive failed sign-ins
    locked_until = db.Column(db.DateTime(timezone=True))  # NULL: not locked

    # ------ The is_active column above replaces UserMixin's is_active, so Flask-Login refuses deactivated accounts. A relationship adds no column: no migration needed. ------
    role = db.relationship("Role", lazy="joined")

    @property
    def initials(self):
        return "".join(part[0] for part in self.name.split()[:2]).upper()
# ------ END APP USER TABLE ------

# ------ 3. FILTER PROFILE ------
class FilterProfile(db.Model):
    __tablename__ = "filter_profile"
    __table_args__ = (
        CheckConstraint("mw_max >= 0", name="mw_max_non_negative"),
        CheckConstraint("hbd_max >= 0", name="hbd_max_non_negative"),
        CheckConstraint("hba_max >= 0", name="hba_max_non_negative"),
        CheckConstraint("tpsa_max >= 0", name="tpsa_max_non_negative"),
        CheckConstraint("rotb_max >= 0", name="rotb_max_non_negative"),
        CheckConstraint("allowed_violations >= 0", name="allowed_violations_non_negative"),
        # Same rules as the form, so no other path can store an invalid profile.
        CheckConstraint("logp_min < logp_max", name="logp_range"),
        CheckConstraint("num_nonnulls(mw_max, logp_min, logp_max, hbd_max, hba_max, tpsa_max, rotb_max) > 0",
                        name="at_least_one_threshold"),
    )

    id = db.Column(db.Integer, Identity(always=True), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("app_user.id"))  # NULL for the preloaded profiles
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    is_official = db.Column(db.Boolean, nullable=False, server_default="false")  # made by a curator, visible to everyone

    # NULL threshold: the profile does not evaluate that descriptor.
    mw_max = db.Column(db.Numeric(10, 2))
    logp_min = db.Column(db.Numeric(6, 2))
    logp_max = db.Column(db.Numeric(6, 2))
    hbd_max = db.Column(db.SmallInteger)
    hba_max = db.Column(db.SmallInteger)
    tpsa_max = db.Column(db.Numeric(7, 2))
    rotb_max = db.Column(db.SmallInteger)

    allowed_violations = db.Column(db.SmallInteger, nullable=False, server_default="0")
    apply_alerts = db.Column(db.Boolean, nullable=False, server_default="true")
    is_active = db.Column(db.Boolean, nullable=False, server_default="true")  # hidden, not deleted: past screenings point to it
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=func.now())
# ------ END FILTER PROFILE TABLE ------

# ------ 4. LIBRARY ------
class Library(db.Model):
    __tablename__ = "library"

    id = db.Column(db.Integer, Identity(always=True), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("app_user.id"), nullable=False, index=True)
    name = db.Column(db.String(150), nullable=False)
    description = db.Column(db.Text)
    uploaded_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=func.now())
# ------ END LIBRARY TABLE ------

# ------ 5. COMPOUND ------
class Compound(db.Model):
    __tablename__ = "compound"

    id = db.Column(db.Integer, Identity(always=True), primary_key=True)
    canonical_smiles = db.Column(db.Text, nullable=False, unique=True)  # deduplication key
    inchikey = db.Column(db.String(27), index=True)
    name = db.Column(db.String(200))

    # Calculated with RDKit. NULL when the compound is out of scope.
    molecular_weight = db.Column(db.Numeric(10, 2))
    clogp = db.Column(db.Numeric(6, 2))
    hbd = db.Column(db.SmallInteger)
    hba = db.Column(db.SmallInteger)
    tpsa = db.Column(db.Numeric(7, 2))
    rotatable_bonds = db.Column(db.SmallInteger)
    aromatic_rings = db.Column(db.SmallInteger)
    heavy_atoms = db.Column(db.SmallInteger, nullable=False)  # always calculated: it decides out_of_scope
    fraction_csp3 = db.Column(db.Numeric(4, 3))
    qed = db.Column(db.Numeric(4, 3))

    out_of_scope = db.Column(db.Boolean, Computed(f"heavy_atoms > {MAX_HEAVY_ATOMS}", persisted=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=func.now())
# ------ END COMPOUND TABLE ------

# ------ 6. COMPOUND LIBRARY ------
class CompoundLibrary(db.Model):
    __tablename__ = "compound_library"

    library_id = db.Column(db.Integer, db.ForeignKey("library.id", ondelete="CASCADE"), primary_key=True)
    compound_id = db.Column(db.Integer, db.ForeignKey("compound.id"), primary_key=True)
    external_code = db.Column(db.String(50))  # the compound's ID in the uploaded file (vendor code, ChEMBL ID…)
# ------ END COMPOUND LIBRARY TABLE ------

# ------ 7. STRUCTURAL ALERT ------
class StructuralAlert(db.Model):
    __tablename__ = "structural_alert"
    __table_args__ = (
        CheckConstraint("family IN ('PAINS_A', 'PAINS_B', 'PAINS_C', 'BRENK', 'REACTIVE')", name="valid_family"),
        CheckConstraint("severity IN ('discard', 'warning')", name="valid_severity"),
    )

    id = db.Column(db.Integer, Identity(always=True), primary_key=True)
    family = db.Column(db.String(20), nullable=False)
    name = db.Column(db.String(150), nullable=False)
    smarts = db.Column(db.Text, nullable=False)
    severity = db.Column(db.String(10), nullable=False, server_default="warning")
    description = db.Column(db.Text)
    reference = db.Column(db.String(255))
    is_active = db.Column(db.Boolean, nullable=False, server_default="true")
# ------ END STRUCTURAL ALERT TABLE ------

# ------ 8. COMPOUND ALERT ------
class CompoundAlert(db.Model):
    __tablename__ = "compound_alert"
    __table_args__ = (
        CheckConstraint("match_count > 0", name="match_count_positive"),
    )

    compound_id = db.Column(db.Integer, db.ForeignKey("compound.id", ondelete="CASCADE"), primary_key=True)
    alert_id = db.Column(db.Integer, db.ForeignKey("structural_alert.id", ondelete="CASCADE"),
                         primary_key=True, index=True)
    match_count = db.Column(db.SmallInteger, nullable=False, server_default="1")
# ------ END COMPOUND ALERT TABLE ------

# ------ 9. SCREENING ------
class Screening(db.Model):
    __tablename__ = "screening"

    id = db.Column(db.Integer, Identity(always=True), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("app_user.id"), nullable=False, index=True)
    library_id = db.Column(db.Integer, db.ForeignKey("library.id", ondelete="CASCADE"), nullable=False)
    filter_profile_id = db.Column(db.Integer, db.ForeignKey("filter_profile.id"), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=func.now())
    total_evaluated = db.Column(db.Integer, nullable=False, server_default="0")
    total_passed = db.Column(db.Integer, nullable=False, server_default="0")
# ------ END SCREENING TABLE ------

# ------ 10. SCREENING RESULT ------
class ScreeningResult(db.Model):
    __tablename__ = "screening_result"

    screening_id = db.Column(db.Integer, db.ForeignKey("screening.id", ondelete="CASCADE"), primary_key=True)
    compound_id = db.Column(db.Integer, db.ForeignKey("compound.id"), primary_key=True)
    passed = db.Column(db.Boolean, nullable=False)
    violations = db.Column(db.SmallInteger, nullable=False, server_default="0")
    reason = db.Column(db.Text)  # e.g. "MW 1202.63 > 500; HBA 12 > 10"
# ------ END SCREENING RESULT TABLE -------

# ------ END ------

# ------ AUTHENTICATION ------
# Shared by the sign-up and change-password forms.
PASSWORD_RULES = [
    Length(min=8, message="Must be at least 8 characters long."),
    Regexp(r".*\d", message="Must include at least one number."),
]

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(AppUser, int(user_id))

def is_curator():
    return current_user.is_authenticated and current_user.role.name == "curator"

@app.context_processor
def inject_role():
    return {"is_curator": is_curator()}

def curator_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not is_curator():
            abort(403)
        return view(*args, **kwargs)
    return wrapped

def safe_next():
    # ------ @login_required needs to be checked to avoid wrong redirections ------
    target = request.args.get("next", "")
    if target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return None

def normalize_email(value):
    return value.strip().lower() if value else value

def empty_to_none(value):
    # ------ A empty field is filled as NULL not as blank ------
    return value.strip() or None if value else None

class LoginForm(FlaskForm):
    email = EmailField("Email", filters=[normalize_email], validators=[DataRequired()])
    password = PasswordField("Password", validators=[DataRequired()])

@app.route("/", methods=["GET", "POST"])
def home():
    if current_user.is_authenticated:
        return redirect(url_for("libraries"))

    form = LoginForm()
    if form.validate_on_submit():
        user = db.session.scalar(select(AppUser).filter_by(email=form.email.data))
        now = datetime.now(timezone.utc)

        if user and user.locked_until and user.locked_until > now:
            form.form_errors.append("Too many failed attempts. Try again in a few minutes.")
        elif user and check_password_hash(user.password_hash, form.password.data):
            if not user.is_active:
                form.form_errors.append("This account is deactivated. Contact a curator.")
            else:
                user.failed_attempts = 0
                user.locked_until = None
                db.session.commit()
                login_user(user)
                return redirect(safe_next() or url_for("libraries"))
        else:
            # ------ Same message for an unknown email and a wrong password, so the form does not reveal which emails have an account. ------
            if user:
                user.failed_attempts += 1
                if user.failed_attempts >= MAX_FAILED_ATTEMPTS:
                    user.locked_until = now + timedelta(minutes=LOCK_MINUTES)
                    user.failed_attempts = 0
                db.session.commit()
            form.form_errors.append("Incorrect email or password.")

    return render_template("home.html", form=form)


class RegisterForm(FlaskForm):
    name = StringField("Name", validators=[DataRequired(), Length(max=100)])
    email = EmailField("Email", filters=[normalize_email], validators=[
        DataRequired(), Length(max=120),
        Regexp(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", message="Enter a valid email address."),
    ])
    institution = StringField("Institution", filters=[empty_to_none], validators=[Optional(), Length(max=150)])
    password = PasswordField("Password", validators=[DataRequired(), *PASSWORD_RULES])
    confirm_password = PasswordField("Confirm password", validators=[
        DataRequired(), EqualTo("password", message="Passwords do not match."),
    ])

    # ------ WTForms runs validate_<field> methods automatically after the field's validators. ------
    def validate_email(self, field):
        if db.session.scalar(select(AppUser.id).filter_by(email=field.data)):
            raise ValidationError("This email is already registered.")

@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("libraries"))

    form = RegisterForm()
    if form.validate_on_submit():
        # ------ Sign-up always creates researchers; curators are promoted with flask --app chemolibrary_triage make-curator EMAIL ------
        user = AppUser(
            name=form.name.data,
            email=form.email.data,
            institution=form.institution.data,
            password_hash=generate_password_hash(form.password.data),
            role=db.session.scalar(select(Role).filter_by(name="researcher")),
        )
        db.session.add(user)
        db.session.commit()
        login_user(user)
        flash("Welcome to ChemoTRIAGE.", "success")
        return redirect(url_for("libraries"))

    return render_template("register.html", form=form)

# ------ POST since any page using a link like https://.../logout will log out any account ------
@app.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    flash("You signed out.", "success")
    return redirect(url_for("home"))


@app.cli.command("make-curator")
@click.argument("email")
def make_curator(email):
    # ------ Give the curator role to an existing account ------
    user = db.session.scalar(select(AppUser).filter_by(email=normalize_email(email)))
    if user is None:
        raise click.ClickException(f"No account uses {email}. Sign up on the site first.")
    user.role = db.session.scalar(select(Role).filter_by(name="curator"))
    db.session.commit()
    click.echo(f"{user.email} is now a curator.")
# ------ END AUTHENTICATION ------

class LibraryForm(FlaskForm):
    name = StringField("Name", validators=[DataRequired(), Length(max=150)])
    description = TextAreaField("Description")
    file = FileField("File", validators=[FileRequired(), FileAllowed(["smi", "csv", "txt"])])

class FilterProfileForm(FlaskForm):
    name = StringField("Name", validators=[DataRequired(), Length(max=100)])
    description = TextAreaField("Description", validators=[Optional()])

    mw_max = DecimalField("Max MW (Da)", places=None,
                          validators=[Optional(), NumberRange(min=0, message="Cannot be negative.")])
    logp_min = DecimalField("Min LogP", places=None, validators=[Optional()])
    logp_max = DecimalField("Max LogP", places=None, validators=[Optional()])
    hbd_max = IntegerField("Max HBD",
                           validators=[Optional(), NumberRange(min=0, message="Cannot be negative.")])
    hba_max = IntegerField("Max HBA",
                           validators=[Optional(), NumberRange(min=0, message="Cannot be negative.")])
    tpsa_max = DecimalField("Max TPSA (Å²)", places=None,
                            validators=[Optional(), NumberRange(min=0, message="Cannot be negative.")])
    rotb_max = IntegerField("Max RotB",
                            validators=[Optional(), NumberRange(min=0, message="Cannot be negative.")])

    allowed_violations = IntegerField("Allowed violations", default=0,
                                      validators=[InputRequired(),
                                                  NumberRange(min=0, message="Cannot be negative.")])
    apply_alerts = BooleanField("Apply structural alerts", default=True)
    is_official = BooleanField("Official profile")

    THRESHOLDS = ("mw_max", "logp_min", "logp_max", "hbd_max", "hba_max", "tpsa_max", "rotb_max")

    # Cross-field rules. They run even when another field already failed, so the
    # user sees every error at once.
    def validate(self, extra_validators=None):
        valid = super().validate(extra_validators)
        if all(getattr(self, name).data is None for name in self.THRESHOLDS):
            self.form_errors.append("Set at least one threshold: a profile without thresholds passes everything.")
            valid = False
        if (self.logp_min.data is not None and self.logp_max.data is not None
                and self.logp_min.data >= self.logp_max.data):
            self.logp_max.errors.append("Must be greater than Min LogP.")
            valid = False
        return valid

OFFICIAL_PROFILES = [
    dict(id=1, name="Lipinski (Ro5)", mw_max=500, logp_min=None, logp_max=5,
         hbd_max=5, hba_max=10, tpsa_max=None, rotb_max=None,
         allowed_violations=1, apply_alerts=False),
    dict(id=2, name="Veber", mw_max=None, logp_min=None, logp_max=None,
         hbd_max=None, hba_max=None, tpsa_max=140, rotb_max=10,
         allowed_violations=0, apply_alerts=False),
    dict(id=3, name="Lipinski + Veber", mw_max=500, logp_min=None, logp_max=5,
         hbd_max=5, hba_max=10, tpsa_max=140, rotb_max=10,
         allowed_violations=1, apply_alerts=True),
    dict(id=4, name="Lead-like", mw_max=350, logp_min=None, logp_max=3.5,
         hbd_max=3, hba_max=6, tpsa_max=120, rotb_max=7,
         allowed_violations=0, apply_alerts=True),
    dict(id=5, name="Fragments (Ro3)", mw_max=300, logp_min=None, logp_max=3,
         hbd_max=3, hba_max=3, tpsa_max=60, rotb_max=3,
         allowed_violations=0, apply_alerts=True),
    dict(id=6, name="beyond Ro5 (bRo5)", mw_max=1500, logp_min=None, logp_max=10,
         hbd_max=10, hba_max=20, tpsa_max=250, rotb_max=25,
         allowed_violations=2, apply_alerts=True),
    dict(id=7, name="CNS penetration", mw_max=400, logp_min=2, logp_max=4,
         hbd_max=3, hba_max=7, tpsa_max=90, rotb_max=8,
         allowed_violations=0, apply_alerts=True),
]

# ------ PROFILE ------

@app.route("/profile", methods=['GET', 'POST'])
@login_required
def profile():
    account_form = AccountForm(obj=current_user)
    if account_form.validate_on_submit():
        taken = db.session.scalar(select(AppUser.id).where(
            AppUser.email == account_form.email.data, AppUser.id != current_user.id))
        if taken:
            account_form.email.errors.append("Another account already uses this email.")
        else:
            account_form.populate_obj(current_user)
            db.session.commit()
            flash("Details updated.", "success")
            return redirect(url_for('profile'))
    return render_template('profile.html', **profile_context(account_form=account_form))


@app.route('/profile/password', methods=['POST'])
@login_required
def change_password():
    password_form = PasswordForm()
    if password_form.validate_on_submit():
        if not check_password_hash(current_user.password_hash, password_form.current_password.data):
            password_form.current_password.errors.append("Current password is incorrect.")
        else:
            current_user.password_hash = generate_password_hash(password_form.new_password.data)
            db.session.commit()
            flash("Password updated.", "success")
            return redirect(url_for('profile'))
    return render_template('profile.html', **profile_context(password_form=password_form))

# ------ User management, curator only. Redirects to the account page until it is built. ------
@app.route('/users')
@curator_required
def users():
    return redirect(url_for('profile'))

class AccountForm(FlaskForm):
    name = StringField("Name", validators=[DataRequired(), Length(max=100)])
    # A regex is enough to check the format; WTForms' Email() needs an extra package.
    email = EmailField("Email", filters=[normalize_email], validators=[
        DataRequired(), Length(max=120),
        Regexp(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", message="Enter a valid email address."),
    ])
    institution = StringField("Institution", filters=[empty_to_none], validators=[Optional(), Length(max=150)])


class PasswordForm(FlaskForm):
    current_password = PasswordField("Current password", validators=[DataRequired()])
    new_password = PasswordField("New password", validators=[DataRequired(), *PASSWORD_RULES])
    confirm_password = PasswordField("Confirm password", validators=[
        DataRequired(), EqualTo("new_password", message="Passwords do not match."),
    ])

    def validate(self, extra_validators=None):
        valid = super().validate(extra_validators)
        if self.new_password.data and self.new_password.data == self.current_password.data:
            self.new_password.errors.append("Must be different from the current password.")
            valid = False
        return valid

def count_own(model):
    return db.session.scalar(select(func.count()).select_from(model).where(model.user_id == current_user.id))

def profile_context(account_form=None, password_form=None):
    """Builds what profile.html needs; used by both profile routes.

    The form that was not submitted gets formdata=None, so it does not read the
    other form's POST data.
    """
    return dict(
        user=current_user,
        initials=current_user.initials,
        role=current_user.role.name.capitalize(),
        stats=dict(libraries=count_own(Library), screenings=count_own(Screening),
                   profiles=count_own(FilterProfile)),
        account_form=account_form if account_form is not None else AccountForm(formdata=None, obj=current_user),
        password_form=password_form if password_form is not None else PasswordForm(formdata=None),
    )

# ----- END PROFILE ------

# ------ SCREENINGS ------

@app.route('/screenings', methods=['GET', 'POST'])
@login_required
def screenings():
    available_libraries = [SimpleNamespace(id=1, name="Demo — reference drugs")]
    profiles = [SimpleNamespace(**p) for p in OFFICIAL_PROFILES]
    demo = SimpleNamespace(id=1, library="Demo — reference drugs",
                           profile="Lipinski + Veber", created_at=datetime(2026, 10, 18, 16, 40),
                           total_evaluated=12, total_passed=7)
    pagination = SimpleNamespace(items=[demo], total=1, pages=1, page=1)
    q = request.args.get('q') or None

    form = ScreeningForm()
    form.library.choices = [("", "Choose a library")] + [(b.id, b.name) for b in available_libraries]
    form.filter_profile.choices = [("", "Choose a profile")] + [(p.id, f"{p.name} (official)") for p in profiles]

    # The "Screen" button in Libraries links here with ?library=<id>.
    if request.method == 'GET':
        form.library.data = request.args.get('library', type=int)

    if form.validate_on_submit():
        # TODO (database): create the screening, evaluate every compound in the
        # library against the profile, store one result per compound, then
        # return redirect(url_for('see_screening', id=screening.id)).
        return redirect(url_for('screenings'))

    return render_template('screenings.html', form=form, pagination=pagination, q=q)

def int_or_none(value):
    """Turns the selected option into an int and the "Choose…" option into None,
    so an empty choice reads "This field is required." instead of a coercion error."""
    return int(value) if value not in (None, "") else None


class ScreeningForm(FlaskForm):
    library = SelectField("Library", coerce=int_or_none, validators=[DataRequired()])
    filter_profile = SelectField("Filter profile", coerce=int_or_none, validators=[DataRequired()])

@app.route('/screenings/<int:id>')
@login_required
def see_screening(id):
    return redirect(url_for('screenings'))

# ------ END SCREENINGS ------



# ------ LIBRARIES ------

@app.route('/libraries')
@login_required
def libraries():
    demo = SimpleNamespace(id=1, name="Demo — reference drugs", uploaded_at=datetime(2026, 10, 18), compound_count=12)
    pagination = SimpleNamespace(items=[demo], total=1, pages=1, page=1)
    return render_template('libraries.html', pagination=pagination, q=None, form=LibraryForm(), summary=None)

@app.route('/libraries/upload', methods=['POST'])
@login_required
def upload_libraries():
    return redirect(url_for('libraries'))

@app.route('/libraries/<int:id>')
@login_required
def see_library(id):
    return redirect(url_for('libraries'))

@app.route('/libraries/<int:id>/delete', methods=['POST'])
@login_required
def delete_library(id):
    return redirect(url_for('libraries'))

# ------ END LIBRARIES ------


# ------ FILTER SETTINGS ------

@app.route('/filter-settings', methods=['GET', 'POST'])
@login_required
def filter_settings():
    official = [SimpleNamespace(is_official=True, description=None, **p) for p in OFFICIAL_PROFILES]
    own = []  # TODO (database): the current user's profiles

    profile_id = request.args.get('profile', type=int)
    selected = next((p for p in official + own if p.id == profile_id), None)

    # Own profiles are edited. Official profiles are edited only by a curator;
    # for everyone else they are the starting point of a new profile.
    editing = selected if selected and (is_curator() or not selected.is_official) else None
    base = selected if selected and not editing else None

    form = FilterProfileForm(obj=selected)
    if request.method == 'GET' and base:
        form.name.data = None  # a new copy: the user names it
        form.is_official.data = False

    if form.validate_on_submit():
        if not is_curator():
            form.is_official.data = False  # hiding the checkbox is not enough
        # TODO (database): create or update the profile with form.populate_obj() and save.
        return redirect(url_for('filter_settings'))

    return render_template('filter-settings.html',
                           official_profiles=official, own_profiles=own, form=form,
                           editing=editing, base=base,
                           selected_id=selected.id if selected else None)

# ------ END FILTER SETTINGS ------


# ------ ALERTS ------

@app.route('/alerts', methods=['GET', 'POST'])
@curator_required
def alerts():
    form = AlertForm()
    preview = None

    if request.method == 'POST':
        if request.form.get('action') == 'preview':
            # Testing a pattern saves nothing: only the SMARTS has to be valid.
            if form.smarts.validate(form):
                preview = preview_pattern(form.smarts.data)
        elif form.validate_on_submit():
            # TODO (database): save the alert, match it against every loaded
            # compound and record the compound_alert rows.
            return redirect(url_for('alerts'))

    # Filtering the demo data; with a database these become query conditions.
    family = request.args.get('family') or None
    q = (request.args.get('q') or '').strip() or None
    all_alerts = [SimpleNamespace(**a) for a in DEMO_ALERTS]

    def group(alert):  # PAINS_A, PAINS_B, PAINS_C → PAINS
        return alert.family.split('_')[0]

    filtered = [a for a in all_alerts
                if (not family or group(a) == family)
                and (not q or q.lower() in a.name.lower())]
    families = [dict(key=None, label="All", count=len(all_alerts))] + [
        dict(key=key, label=label, count=sum(group(a) == key for a in all_alerts))
        for key, label in (("PAINS", "PAINS"), ("BRENK", "BRENK"), ("REACTIVE", "Reactive"))
    ]
    catalog = SimpleNamespace(items=filtered, total=len(filtered), pages=1, page=1)

    return render_template('alerts.html', catalog=catalog, families=families,
                           family=family, q=q, form=form, preview=preview)

@app.route('/alerts/<int:id>/toggle', methods=['POST'])
@curator_required
def toggle_alert(id):
    # TODO (database): flip alert.active and save.
    return redirect(url_for('alerts'))

def valid_smarts(form, field):
    """Compiles the pattern with RDKit and shows RDKit's own message when it fails.

    RDKit does not raise on an invalid SMARTS: it returns None and logs the
    reason to stderr, which is captured here.
    """
    from rdkit import Chem, rdBase

    rdBase.LogToPythonStderr()
    output = io.StringIO()
    with contextlib.redirect_stderr(output):
        pattern = Chem.MolFromSmarts(field.data)
    if pattern is None:
        lines = output.getvalue().strip().splitlines()
        detail = lines[0].split("Parse Error:", 1)[-1].strip() if lines else ""
        raise ValidationError("Invalid SMARTS pattern." + (f" RDKit says: {detail}" if detail else ""))


class AlertForm(FlaskForm):
    family = SelectField("Family", default="REACTIVE", choices=[
        ("PAINS_A", "PAINS A"), ("PAINS_B", "PAINS B"), ("PAINS_C", "PAINS C"),
        ("BRENK", "BRENK"), ("REACTIVE", "Reactive"),
    ])
    # New alerts start as warnings: discarding compounds should be a deliberate choice.
    severity = SelectField("Severity", default="warning", choices=[
        ("discard", "Discard"), ("warning", "Warning"),
    ])
    name = StringField("Name", validators=[DataRequired(), Length(max=150)])
    smarts = TextAreaField("SMARTS pattern", validators=[DataRequired(), valid_smarts])
    description = TextAreaField("Description", validators=[Optional()])
    reference = StringField("Reference", validators=[Optional(), Length(max=255)])

# TEST ONLY: ALERTS

DEMO_ALERTS = [
    dict(id=1, family="PAINS_A", name="ene_rhod_A(235)", severity="discard", matches=1, active=True),
    dict(id=2, family="BRENK", name="isolated_alkene", severity="warning", matches=2, active=True),
    dict(id=3, family="BRENK", name="phenol_ester", severity="warning", matches=1, active=True),
    dict(id=4, family="BRENK", name="hydroquinone", severity="warning", matches=1, active=True),
    dict(id=5, family="BRENK", name="imine_1", severity="warning", matches=1, active=True),
    dict(id=6, family="BRENK", name="imine_2", severity="warning", matches=1, active=True),
    dict(id=7, family="BRENK", name="diketo_group", severity="warning", matches=1, active=True),
    dict(id=8, family="BRENK", name="Michael_acceptor_1", severity="warning", matches=1, active=True),
    dict(id=9, family="BRENK", name="Thiocarbonyl_group", severity="warning", matches=1, active=True),
    dict(id=10, family="REACTIVE", name="aldehyde", severity="warning", matches=0, active=False),
]

DEMO_SMI_PATH = Path(app.root_path) / "data" / "demo_compounds.smi"


def read_smi(path):
    """Reads a .smi file: one SMILES per line, optionally followed by a name.
    Blank lines and lines starting with # are skipped."""
    compounds = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        parts = line.split(maxsplit=1)
        if parts and not parts[0].startswith("#"):
            compounds.append((parts[0], parts[1] if len(parts) > 1 else parts[0]))
    return compounds


def preview_pattern(smarts):
    """Matches the pattern against the loaded compounds without saving anything."""
    from rdkit import Chem

    pattern = Chem.MolFromSmarts(smarts)
    compounds = read_smi(DEMO_SMI_PATH)
    matches = []
    for smiles, name in compounds:
        mol = Chem.MolFromSmiles(smiles)
        if mol is not None and mol.HasSubstructMatch(pattern):
            matches.append(name)
    return {"matches": matches, "total": len(compounds)}

# ------ END ALERTS -------

if __name__ == "__main__":
    app.run(debug=True)

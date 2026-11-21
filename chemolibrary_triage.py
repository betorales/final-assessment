import contextlib
import io
import os
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from types import SimpleNamespace

# ----- FLASK APP ------
import click
from flask import Flask, abort, flash, redirect, render_template, request, url_for, session
from flask_login import LoginManager, UserMixin, current_user, login_required, login_user, logout_user
from flask_wtf import CSRFProtect, FlaskForm
from flask_wtf.file import FileAllowed, FileField, FileRequired
from werkzeug.security import check_password_hash, generate_password_hash
from wtforms import (BooleanField, DecimalField, EmailField, IntegerField, PasswordField, SelectField, StringField, TextAreaField)
from wtforms.validators import (DataRequired, EqualTo, InputRequired, Length, NumberRange, Optional, Regexp, ValidationError)

# ----- DATABASE -----
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import CheckConstraint, Computed, Identity, MetaData, func, select, or_, delete

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

# Shown rejections
MAX_SHOWN_REJECTIONS = 10

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
    # ------ Show user's libraries: counting compounds ------
    @property
    def compound_count(self):
        return db.session.scalar(select(func.count()).select_from(CompoundLibrary).where(CompoundLibrary.library_id==self.id))
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

    """
    This allows that a compound knows what alerts has.
    Also, simplify the Neon process using selectin
    """
    alerts = db.relationship("StructuralAlert", secondary="compound_alert", viewonly=True, lazy="selectin")
# ------ END COMPOUND TABLE ------

# ------ 6. COMPOUND LIBRARY ------
class CompoundLibrary(db.Model):
    __tablename__ = "compound_library"

    library_id = db.Column(db.Integer, db.ForeignKey("library.id", ondelete="CASCADE"), primary_key=True)
    compound_id = db.Column(db.Integer, db.ForeignKey("compound.id"), primary_key=True)
    external_code = db.Column(db.String(50))  # the compound's ID in the uploaded file (vendor code, ChEMBL ID…)
    compound = db.relationship("Compound", lazy="joined")
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

    @property
    def compound_count(self):
        return db.session.scalar(select(func.count()).select_from(CompoundAlert).where(CompoundAlert.alert_id==self.id))
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

    # ------ Allows to retrieve everything from these tables (LEFT OUTER JOIN) ------
    library = db.relationship("Library", lazy="joined")
    filter_profile = db.relationship("FilterProfile", lazy="joined")
# ------ END SCREENING TABLE ------

# ------ 10. SCREENING RESULT ------
class ScreeningResult(db.Model):
    __tablename__ = "screening_result"

    screening_id = db.Column(db.Integer, db.ForeignKey("screening.id", ondelete="CASCADE"), primary_key=True)
    compound_id = db.Column(db.Integer, db.ForeignKey("compound.id"), primary_key=True)
    passed = db.Column(db.Boolean, nullable=False)
    violations = db.Column(db.SmallInteger, nullable=False, server_default="0")
    reason = db.Column(db.Text)  # e.g. "MW 1202.63 > 500; HBA 12 > 10"

    compound = db.relationship("Compound", lazy="joined")
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

# ------ Upload official profiles as table ------
@app.cli.command("seed-profiles")
def seed_profiles():
    profiles_created = 0
    for profile in OFFICIAL_PROFILES:
        data = dict(profile)
        del data["id"]
        existing = db.session.scalar(select(FilterProfile).filter_by(name=data["name"], is_official=True))
        if existing is None:
            profiles_created += 1
            filter_profile=FilterProfile(
                is_official=True,
                **data
        )
            db.session.add(filter_profile)  
    click.echo(f"Created {profiles_created} official profiles")
    db.session.commit()

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
    # Select users from AppUser
    query=select(AppUser).order_by(AppUser.name)
    q = request.args.get("q", "").strip() or None
    if q:
        query = query.where(or_(AppUser.name.ilike(f"%{q}%"), AppUser.email.ilike(f"%{q}%")))
    pagination=db.paginate(query, per_page=20)
    return render_template('users.html', pagination=pagination, q=q)

@app.route('/users/<int:id>/toggle', methods=['POST'])
@curator_required
def toggle_user(id):
    user=db.get_or_404(AppUser, id)
    if user.id==current_user.id:
        flash("You can’t deactivate your own account")
        return redirect(url_for('users'))
    user.is_active=not user.is_active
    db.session.commit()
    state = "activated" if user.is_active else "deactivated"
    flash(f"The following account {user.name} has been {state}", "success")
    return redirect(url_for('users')) 


@app.route('/users/<int:id>/role', methods=['POST'])
@curator_required
def change_role(id):
    user=db.get_or_404(AppUser, id)
    if user.id==current_user.id:
        flash("You can't reassign your role")
        return redirect(url_for('users'))
    new_role="researcher" if user.role.name == "curator" else "curator"
    user.role = db.session.scalar(select(Role).filter_by(name=new_role))
    db.session.commit()
    flash(f"The user {user.name} has changed role to {new_role}", 'success')
    return redirect(url_for('users'))

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
    # ------ Adding options to library dropdown in Screenings view ------
    available_libraries = db.session.scalars(select(Library).filter_by(user_id=current_user.id).order_by(Library.name)).all()
    official, own = visible_profiles()
    # ------ Show user's screenings ------
    query=select(Screening).filter_by(user_id=current_user.id).order_by(Screening.created_at.desc())
    q = request.args.get("q", "").strip() or None
    if q:
        query = query.join(Screening.library).join(Screening.filter_profile).where(
        or_(Library.name.ilike(f"%{q}%"), FilterProfile.name.ilike(f"%{q}%"))
        )
    pagination = db.paginate(query, per_page=10)
    form = ScreeningForm()
    form.library.choices = [("", "Choose a library")] + [(b.id, b.name) for b in available_libraries]
    official_profiles = [(p.id, f"{p.name} (Official profile)") for p in official]
    own_profiles = [(p.id, p.name) for p in own]
    form.filter_profile.choices = [("", "Choose a profile")] + official_profiles + own_profiles

    # The "Screen" button in Libraries links here with ?library=<id>.
    if request.method == 'GET':
        form.library.data = request.args.get('library', type=int)

    if form.validate_on_submit():
        library = db.session.get(Library, form.library.data)
        profile = db.session.get(FilterProfile, form.filter_profile.data)
        # ------ To create a screening ------
        screening = Screening(
            user_id=current_user.id,
            library_id=library.id,
            filter_profile_id=profile.id
        )
        db.session.add(screening)
        db.session.flush()
        # ------ Compounds in the chosen library ------
        compounds = db.session.scalars(select(Compound).join(CompoundLibrary).where(CompoundLibrary.library_id == library.id)).all()
        total_passed = 0
        # ------ Evaluate and store one result per compound ------
        for compound in compounds:
            passed, violations, reason = evaluate(compound, profile)
            if passed:
                total_passed += 1
            screening_result=ScreeningResult(
                screening_id=screening.id,
                compound_id=compound.id,
                passed=passed,
                violations=violations,
                reason=reason
            )
            db.session.add(screening_result)

        total_evaluated=len(compounds)
        screening.total_evaluated=total_evaluated
        screening.total_passed=total_passed
        db.session.commit()
        return redirect(url_for('see_screening', id=screening.id))

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
    screening = db.get_or_404(Screening, id)
    if screening.user_id != current_user.id:
        abort(404)

    query=select(ScreeningResult).filter_by(screening_id=screening.id).order_by(ScreeningResult.compound_id)
    pagination = db.paginate(query, per_page=20)
    
    return render_template('screening.html', screening=screening, pagination=pagination)

# ------ END SCREENINGS ------

# ------ LIBRARIES ------

@app.route('/libraries')
@login_required
def libraries():
    q = request.args.get("q","").strip() or None
    pagination= user_libraries(q)
    # ------ Keeping summary in screen ------
    summary = session.pop("upload_summary", None)
    return render_template('libraries.html', pagination=pagination, q=q, form=LibraryForm(), summary=summary)

@app.route('/libraries/upload', methods=['POST'])
@login_required
def upload_libraries():
    form = LibraryForm()
    if form.validate_on_submit():
        uploaded_file = form.file.data
        # ------ Some files require another utf-8, like Windows devices ------
        import time
        t0 = time.perf_counter() # TP
        text = uploaded_file.read().decode("utf-8-sig")
        compounds = read_smi(text)
        valid, rejections = validate_compounds(compounds)
        unique, duplicates = deduplicate(valid)
        described = []
        out_of_scope = 0
        patterns = compile_alerts()
        for  number, canonical, name, mol in unique:
            descriptors = describe(mol)
            if descriptors["heavy_atoms"] > MAX_HEAVY_ATOMS:
                out_of_scope += 1
            described.append((number, canonical, name, descriptors, mol))
        # ------ Create a library ------
        t1 = time.perf_counter() # TP
        library = Library(
            user_id=current_user.id,
            name=form.name.data,
            description=form.description.data
        )
        db.session.add(library)
        db.session.flush()
        alerts_time = 0 # TP
        # ------ Search for or create a compound ------
        for number, canonical, name, descriptors, mol in described:
            compound = db.session.scalar(select(Compound).filter_by(canonical_smiles=canonical))
            if compound is None:
                compound = Compound(
                    canonical_smiles=canonical, 
                    name=name, 
                    **descriptors
                )
                db.session.add(compound)
                db.session.flush()
                # ------ New compounds in scope: record their structural alerts ------
                if not compound.out_of_scope:
                    ta = time.perf_counter()
                    matches = match_alerts(mol, patterns)
                    alerts_time+=time.perf_counter()-ta
                    for alert_id, match_count in matches:
                        compound_alert=CompoundAlert(
                            compound_id=compound.id,
                            alert_id=alert_id,
                            match_count=match_count
                        )
                        db.session.add(compound_alert)
            # ------ Link to library. Several libraries, not several compounds  ------
            compound_library = CompoundLibrary(
                library_id=library.id,
                compound_id=compound.id,
                external_code=name
            )
            db.session.add(compound_library)
        db.session.commit()
        t2 = time.perf_counter() # TP
        app.logger.warning(f"Upload of {len(described)}: descriptors {t1 - t0:.1f}s, alerts {alerts_time:.1f}s, DB {t2 - t1 - alerts_time:.1f}s")
        # ------ Creating a summary ------
        summary = {"name":library.name, "loaded":len(described), "duplicates":len(duplicates), "invalid":len(rejections), "out_of_scope":out_of_scope, "rejections":(duplicates + rejections)[:MAX_SHOWN_REJECTIONS]}
        # ------ Keeping summary in screen ------
        session["upload_summary"] = summary
        return redirect(url_for('libraries'))
    else:
        pagination=user_libraries(None)
        return render_template('libraries.html', pagination=pagination, q=None, form=form, summary=None)

@app.route('/libraries/<int:id>')
@login_required
def see_library(id):
    library = db.get_or_404(Library, id)
    if library.user_id != current_user.id:
        abort(404)

    query = select(CompoundLibrary).filter_by(library_id=library.id).order_by(CompoundLibrary.compound_id)
    pagination=db.paginate(query, per_page=10)
    return render_template('library.html', library=library, pagination=pagination)

@app.route('/libraries/<int:id>/delete', methods=['POST'])
@login_required
def delete_library(id):
    # ------ Get the library to be deleted ------
    library = db.get_or_404(Library, id)
    if library.user_id != current_user.id:
        abort(404)

    db.session.delete(library)
    db.session.commit()
    flash("Library deleted", "success")
    
    return redirect(url_for('libraries'))

# ------ END LIBRARIES ------


# ------ FILTER SETTINGS ------

@app.route('/filter-settings', methods=['GET', 'POST'])
@login_required
def filter_settings():
    # ------ Query to FilterProfile filtering is_official and is_active, showing Offcial profiles from DB ------
    official, own = visible_profiles()
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

        if editing:
            # ------ Not selected, since user might own ones or uses officials ------
            profile = editing
        else:
            # ------ Create a personal profile for a user ------
            profile = FilterProfile(
                user_id=current_user.id
            )
        form.populate_obj(profile)
        db.session.add(profile)
        # ------ Prevents ownership of a official profile (current user) ------
        if profile.user_id is None and not profile.is_official:
            profile.user_id=current_user.id
        db.session.commit()
        flash("Profile saved", "success")

        return redirect(url_for('filter_settings', profile=profile.id))

    return render_template('filter-settings.html',
                           official_profiles=official, own_profiles=own, form=form,
                           editing=editing, base=base,
                           selected_id=selected.id if selected else None)

# ------ END FILTER SETTINGS ------


# ------ ALERTS ------

@app.route('/alerts', methods=['GET', 'POST'])
@curator_required
def alerts():
    from rdkit import Chem
    form = AlertForm()
    preview = None

    if request.method == 'POST':
        if request.form.get('action') == 'preview':
            # Testing a pattern saves nothing: only the SMARTS has to be valid.
            if form.smarts.validate(form):
                preview = preview_pattern(form.smarts.data)
        elif form.validate_on_submit():
            # Save an alert, match it against every loaded
            # compound and record the compound_alert rows.
            alert=StructuralAlert()
            form.populate_obj(alert)
            db.session.add(alert)
            db.session.flush()
            pattern = Chem.MolFromSmarts(alert.smarts, mergeHs=True)
            compounds = db.session.scalars(select(Compound).filter_by(out_of_scope=False)).all()
            for compound in compounds:
                mol = Chem.MolFromSmiles(compound.canonical_smiles)
                matches = match_alerts(mol, [(alert.id, pattern)])
                for alert_id, match_count in matches:
                    compound_alert=CompoundAlert(
                        compound_id=compound.id,
                        alert_id=alert_id,
                        match_count=match_count
                    )
                    db.session.add(compound_alert)
            db.session.commit()
            return redirect(url_for('alerts'))

    # Filtering the demo data; with a database these become query conditions.
    family = request.args.get('family') or None
    q = (request.args.get('q') or '').strip() or None
    query = select(StructuralAlert).order_by(StructuralAlert.family, StructuralAlert.name)
    
    if family:
        query = query.where(StructuralAlert.family.startswith(family))

    if q:
        query = query.where(StructuralAlert.name.ilike(f"%{q}%"))
    
    catalog=db.paginate(query, per_page=20)

    families = []
    families.append(dict(key=None, label="All", count=db.session.scalar(select(func.count()).select_from(StructuralAlert))))
    for key, label in (("PAINS", "PAINS"), ("BRENK", "BRENK"), ("REACTIVE", "Reactive")):
        families.append(dict(key=key, label=label, count=db.session.scalar(select(func.count()).select_from(StructuralAlert).where(StructuralAlert.family.startswith(key)))))

    return render_template('alerts.html', catalog=catalog, families=families,
                           family=family, q=q, form=form, preview=preview)

@app.route('/alerts/<int:id>/toggle', methods=['POST'])
@curator_required
def toggle_alert(id):
    alert=db.get_or_404(StructuralAlert, id)
    alert.is_active=not alert.is_active
    db.session.commit()
    state = "activated" if alert.is_active else "deactivated"
    flash(f"Alert {alert.name }has changed: {state}", "success")
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

    def validate_name(self, field):
        if db.session.scalar(select(StructuralAlert.id).filter_by(name=field.data)):
            raise ValidationError("This name is already used.")

# ------ Add alerts ------
@app.cli.command("seed-alerts")
def seed_alerts():
    from rdkit.Chem.FilterCatalog import FilterCatalog, FilterCatalogParams
    from rdkit import RDConfig
    import csv

    families = {}
    # ------ Adding alerts from catalog ------
    for family in ("PAINS_A", "PAINS_B", "PAINS_C"):
        params = FilterCatalogParams()
        params.AddCatalog(getattr(FilterCatalogParams.FilterCatalogs, family))
        catalog = FilterCatalog(params)
        for i in range(catalog.GetNumEntries()):
            name = catalog.GetEntryWithIdx(i).GetDescription()
            families[name] = family
    pains_file_route = Path(RDConfig.RDDataDir)/"Pains"/"wehi_pains.csv"
    alerts_created = 0
    with pains_file_route.open(mode="r", encoding="utf-8") as pains_file:
        reader = csv.reader(pains_file)
        for smarts, reg_id in reader:
            name = reg_id.removeprefix("<regId=").removesuffix(">")
            existing = db.session.scalar(select(StructuralAlert).filter_by(name=name))
            if existing is None:
                alerts_created += 1
                structural_alert=StructuralAlert(
                    family=families[name],
                    name=name,
                    smarts=smarts,
                    severity="discard",
                    reference="Baell & Holloway, J Med Chem 2010. doi:10.1021/jm901137j"
                )
                db.session.add(structural_alert)
    db.session.commit()
    click.echo(f"Created {alerts_created} PAINS alerts")
# ------ END ALERTS -------

# ------ Rebuild alerts if they were created previous any action ------
@app.cli.command("rebuild-alerts")
def rebuild_compound_alerts():
    from rdkit import Chem

    db.session.execute(delete(CompoundAlert))
    patterns = compile_alerts()
    compounds = db.session.scalars(select(Compound).filter_by(out_of_scope=False)).all()
    rows_created = 0
    for compound in compounds:
        mol = Chem.MolFromSmiles(compound.canonical_smiles)
        matches = match_alerts(mol, patterns)
        for alert_id, match_count in matches:
            rows_created += 1
            compound_alert=CompoundAlert(
                compound_id=compound.id,
                alert_id=alert_id,
                match_count=match_count
            )
            db.session.add(compound_alert)
    db.session.commit()   
    click.echo(rows_created)
 
# ------ Other functions ------
def validate_compounds(compounds):
    # ------ Extract valid and rejected SMILES ------
    from rdkit import Chem, rdBase
    rdBase.LogToPythonStderr()
    valid = []
    rejections = []
    for number, smiles, name in compounds:
        
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            # ------ Read Stderr so error can be appended ans shown to user ------
            lines = output.getvalue().strip().splitlines()
            # ------ Remove Parse Error to provide clearer info ------ 
            reason = lines[0].split("]", 1)[-1].strip().removeprefix("SMILES Parse Error: ") if lines else "Invalid SMILES"
            rejections.append({"line": number, "reason": f"Invalid SMILES: {reason}"})
        else:
            valid.append((number, smiles, name, mol))
    return valid, rejections

def deduplicate(valid):
    from rdkit import Chem
    # ------ Uniques and/or first SMILES usage ------
    unique = []
    # ------ SMILES already uploaded ------
    duplicates = []
    # ------ SMILES already exists in this file ------
    seen = {}
    for number, smiles, name, mol in valid:
        canonical = Chem.MolToSmiles(mol)
        if canonical in seen:
            duplicates.append({"line": number, "reason":f"Duplicate of line {seen[canonical]}"})
        else:
            unique.append((number, canonical, name, mol))
            seen[canonical] = number
    return unique, duplicates

def read_smi(text):
    """Reads text from .smi: one SMILES per line, optionally followed by a name.
    Blank lines and lines starting with # are skipped."""
    compounds = []
    for number, line in enumerate(text.splitlines(), start = 1):
        # ------ Line numbers include blank lines and comments, so they match the user's editor ------
        line = line.strip()
        if not line:
            continue
        parts = line.split(maxsplit=1)
        if parts and not parts[0].startswith("#"):
            compounds.append((number, parts[0], parts[1] if len(parts) > 1 else None))
    return compounds

# ------ Profiles a user can see: the official ones and their own ------
def visible_profiles():
    official = db.session.scalars(select(FilterProfile).filter_by(is_official=True, is_active=True).order_by(FilterProfile.name)).all()
    own = db.session.scalars(select(FilterProfile).filter_by(user_id=current_user.id, is_official=False, is_active=True).order_by(FilterProfile.name)).all()
    return official, own

# ------ Returns: column name → value, ready for Compound(**descriptors) ------
def describe(mol):
    from rdkit import Chem
    from rdkit.Chem import Descriptors, QED
    descriptors = {}
    mol_heavy_atoms = mol.GetNumHeavyAtoms()
    descriptors["heavy_atoms"]= mol_heavy_atoms

    if mol_heavy_atoms <= MAX_HEAVY_ATOMS:
        descriptors["inchikey"]=Chem.MolToInchiKey(mol)
        descriptors["molecular_weight"]=Descriptors.MolWt(mol)
        descriptors["clogp"]=Descriptors.MolLogP(mol)
        descriptors["hbd"]=Descriptors.NumHDonors(mol) 
        descriptors["hba"]=Descriptors.NumHAcceptors(mol)
        descriptors["tpsa"]=Descriptors.TPSA(mol)
        descriptors["rotatable_bonds"]=Descriptors.NumRotatableBonds(mol)
        descriptors["aromatic_rings"]=Descriptors.NumAromaticRings(mol)
        descriptors["fraction_csp3"]=Descriptors.FractionCSP3(mol)
        descriptors["qed"]=QED.qed(mol)
    return descriptors

# ------ To evaluate compounds vs profile ------
def evaluate(compound, profile):
    if compound.out_of_scope:
        return False, 0, f"Out of scope: {compound.heavy_atoms} heavy atoms > {MAX_HEAVY_ATOMS}."
    reasons = []
    if profile.mw_max is not None and compound.molecular_weight > profile.mw_max:
        reasons.append(f"MW: {compound.molecular_weight} > {profile.mw_max}")
    if profile.logp_min is not None and compound.clogp < profile.logp_min:
        reasons.append(f"LogP: {compound.clogp} < {profile.logp_min}")
    if profile.logp_max is not None and compound.clogp > profile.logp_max:
        reasons.append(f"LogP: {compound.clogp} > {profile.logp_max}")
    if profile.hbd_max is not None and compound.hbd > profile.hbd_max:
        reasons.append(f"HBD: {compound.hbd} > {profile.hbd_max}")
    if profile.hba_max is not None and compound.hba > profile.hba_max:
        reasons.append(f"HBA: {compound.hba} > {profile.hba_max}")
    if profile.tpsa_max is not None and compound.tpsa > profile.tpsa_max:
        reasons.append(f"TPSA: {compound.tpsa} > {profile.tpsa_max}")
    if profile.rotb_max is not None and compound.rotatable_bonds > profile.rotb_max:
        reasons.append(f"Rotatable bonds: {compound.rotatable_bonds} > {profile.rotb_max}")
    # ------ Counted before the alerts: an alert is not a rule violation ------
    violations = len(reasons)
    discarded = False
    # ------ Add alerts to reasons (Results) ------
    if profile.apply_alerts:
        for alert in compound.alerts:
            if alert.is_active:
                reasons.append(f"{alert.family}: {alert.name}")
                if alert.severity == "discard":
                    discarded = True
    passed = violations <= profile.allowed_violations and not discarded
    if reasons:
        reason = "; ".join(reasons)
    else:
        reason = None
    return passed, violations, reason

# ------ Helper: returns all structural alerts and return a list ------
def compile_alerts():
    from rdkit import Chem

    patterns = []
    alerts = db.session.scalars(select(StructuralAlert)).all()
    for alert in alerts:
        pattern = Chem.MolFromSmarts(alert.smarts, mergeHs=True)
        patterns.append((alert.id, pattern))
    return patterns

# ------ Helper: finds user's libraries ------
def user_libraries(q):
    # ------ Show user's libraries and find matching libraries name ------
    query = select(Library).filter_by(user_id=current_user.id).order_by(Library.uploaded_at.desc())
    # ------ Filter ONLY if there is something to filter ------
    if q:
        query = query.where(Library.name.ilike(f"%{q}%"))
    pagination = db.paginate(query, per_page=10)
    return pagination

# ------ Helper: matches a molecule and patterns ------    
def match_alerts(mol, patterns):
    matches = []
    for alert_id, pattern in patterns:
        len_matches = len(mol.GetSubstructMatches(pattern))
        if len_matches > 0:
            matches.append((alert_id, len_matches))
    return matches

def preview_pattern(smarts):
    """Matches the pattern against the loaded compounds without saving anything."""
    from rdkit import Chem

    pattern = Chem.MolFromSmarts(smarts, mergeHs=True)
    compounds = db.session.scalars(select(Compound).filter_by(out_of_scope=False)).all()
    matches = []
    for compound in compounds:
        mol = Chem.MolFromSmiles(compound.canonical_smiles)
        if mol.HasSubstructMatch(pattern):
            matches.append(compound.name)
    return {"matches": matches, "total": len(compounds)}

if __name__ == "__main__":
    app.run(debug=True)

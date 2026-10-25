import contextlib
import io
import os
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from flask import Flask, abort, flash, redirect, render_template, request, url_for
from flask_wtf import CSRFProtect, FlaskForm
from flask_wtf.file import FileAllowed, FileField, FileRequired
from werkzeug.security import check_password_hash, generate_password_hash
from wtforms import (BooleanField, DecimalField, EmailField, IntegerField, PasswordField,
                     SelectField, StringField, TextAreaField)
from wtforms.validators import (DataRequired, EqualTo, InputRequired, Length, NumberRange,
                                Optional, Regexp, ValidationError)

app = Flask(__name__)

# ------ APP CONFIG ------
# In production the key comes from the environment; the fallback is for local use only.
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-only-change-me")
CSRFProtect(app)
# ------ END ------

# ------ TEST ONLY: CURATOR PROFILE ------
CURATOR_MODE = True

def is_curator():
    return CURATOR_MODE

@app.context_processor
def inject_role():
    return {"is_curator": is_curator()}

# ----- END ------

class LibraryForm(FlaskForm):
    name = StringField("Name", validators=[DataRequired(), Length(max=150)])
    description = TextAreaField("Description")
    file = FileField("File", validators=[FileRequired(), FileAllowed(["smi", "csv", "txt"])])

class FilterProfileForm(FlaskForm):
    name = StringField("Name", validators=[DataRequired(), Length(max=100)])
    description = TextAreaField("Description", validators=[Optional()])

    # Optional(): an empty field arrives as None and is stored as NULL, which in
    # the data model means "this profile does not evaluate the descriptor".
    # places=None shows "400" instead of "400.00".
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

    # InputRequired, not DataRequired: DataRequired rejects 0, which is valid here.
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

@app.route("/")
def home():
    return render_template('home.html')

# ------ PROFILE ------

@app.route("/profile", methods=['GET', 'POST'])
def profile():
    account_form = AccountForm(obj=SimpleNamespace(**DEMO_USER))
    if account_form.validate_on_submit():
        # TODO (database): check the email is not used by another account (it is
        # UNIQUE), then account_form.populate_obj(user) and save.
        flash("Details updated.", "success")
        return redirect(url_for('profile'))
    return render_template('profile.html', **profile_context(account_form=account_form))


@app.route('/profile/password', methods=['POST'])
def change_password():
    user = SimpleNamespace(**DEMO_USER)
    password_form = PasswordForm()
    if password_form.validate_on_submit():
        if not check_password_hash(user.password_hash, password_form.current_password.data):
            password_form.current_password.errors.append("Current password is incorrect.")
        else:
            # TODO (database): user.password_hash = generate_password_hash(password_form.new_password.data)
            flash("Password updated.", "success")
            return redirect(url_for('profile'))
    return render_template('profile.html', **profile_context(password_form=password_form))

# User management, curator only. Redirects to the account page until it is built.
@app.route('/users')
def users():
    if not is_curator():
        abort(403)
    return redirect(url_for('profile'))

class AccountForm(FlaskForm):
    name = StringField("Name", validators=[DataRequired(), Length(max=100)])
    # A regex is enough to check the format; WTForms' Email() needs an extra package.
    email = EmailField("Email", validators=[
        DataRequired(), Length(max=120),
        Regexp(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", message="Enter a valid email address."),
    ])
    institution = StringField("Institution", validators=[Optional(), Length(max=150)])


# Shared with the sign-up form.
PASSWORD_RULES = [
    Length(min=8, message="Must be at least 8 characters long."),
    Regexp(r".*\d", message="Must include at least one number."),
]


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

DEMO_USER = dict(
    id=1,
    name="Demo User",
    email="demo@example.org",
    institution="Example University",
    created_at=datetime(2026, 10, 1),
    password_hash=generate_password_hash("demo1234"),
)


def profile_context(account_form=None, password_form=None):
    """Builds what profile.html needs; used by both profile routes.

    The form that was not submitted gets formdata=None, so it does not read the
    other form's POST data.
    """
    user = SimpleNamespace(**DEMO_USER)
    return dict(
        user=user,
        initials="".join(part[0] for part in user.name.split()[:2]).upper(),
        role="Curator" if is_curator() else "Researcher",
        stats=dict(libraries=1, screenings=1, profiles=0),  # TODO (database): COUNT queries
        account_form=account_form if account_form is not None else AccountForm(formdata=None, obj=user),
        password_form=password_form if password_form is not None else PasswordForm(formdata=None),
    )

# ----- END PROFILE ------

# ------ SCREENINGS ------

@app.route('/screenings', methods=['GET', 'POST'])
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
def see_screening(id):
    return redirect(url_for('screenings'))

# ------ END SCREENINGS ------



# ------ LIBRARIES ------

@app.route('/libraries')
def libraries():
    demo = SimpleNamespace(id=1, name="Demo — reference drugs", uploaded_at=datetime(2026, 10, 18), compound_count=12)
    pagination = SimpleNamespace(items=[demo], total=1, pages=1, page=1)
    return render_template('libraries.html', pagination=pagination, q=None, form=LibraryForm(), summary=None)

@app.route('/libraries/upload', methods=['POST'])
def upload_libraries():
    return redirect(url_for('libraries'))

@app.route('/libraries/<int:id>')
def see_library(id):
    return redirect(url_for('libraries'))

@app.route('/libraries/<int:id>/delete', methods=['POST'])
def delete_library(id):
    return redirect(url_for('libraries'))

# ------ END LIBRARIES ------


# ------ FILTER SETTINGS ------

@app.route('/filter-settings', methods=['GET', 'POST'])
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
def alerts():
    if not is_curator():
        abort(403)

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
def toggle_alert(id):
    if not is_curator():
        abort(403)
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

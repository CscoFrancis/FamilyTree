import hashlib
import hmac
import re
import time
from html import escape as esc
import numpy as np
import pandas as pd
import streamlit as st
import gspread
from google.oauth2.service_account import Credentials

st.set_page_config(page_title="Family Tree Viewer", layout="wide")

APP_TITLE = "🌳 Family Tree Viewer"

# ---------------------------------------------------------------------------
# Passcode gate: nothing below this block runs (no data is even fetched from
# the sheet) until the correct passcode has been entered.
# ---------------------------------------------------------------------------
try:
    PASSCODE = st.secrets["auth"]["passcode"]
except Exception:  # no secrets file, no [auth] block, or no passcode key
    PASSCODE = None

# Optional: a long random string that signs the "remember me" cookie. Without
# it the app still works, but users must re-enter the passcode after a refresh.
# Generate one with:  python -c "import secrets; print(secrets.token_hex(32))"
try:
    COOKIE_SECRET = st.secrets["auth"]["cookie_secret"]
except Exception:
    COOKIE_SECRET = None

COOKIE_NAME = "ft_auth"
REMEMBER_DAYS = 2

if not PASSCODE:
    st.title(APP_TITLE)
    st.error(
        "No passcode configured. Add `[auth]` with `passcode = \"...\"` to "
        "`.streamlit/secrets.toml` and restart the app."
    )
    st.stop()


def _sign(expiry):
    # The key mixes in the passcode, so changing the passcode instantly
    # invalidates every cookie that was issued before.
    key = f"{COOKIE_SECRET}|{PASSCODE}".encode("utf-8")
    return hmac.new(key, str(expiry).encode("utf-8"), hashlib.sha256).hexdigest()


def make_token():
    """A signed, expiring token. It contains no passcode, only an expiry time
    and a signature that can't be forged without COOKIE_SECRET."""
    expiry = int(time.time()) + REMEMBER_DAYS * 86400
    return f"{expiry}.{_sign(expiry)}"


def token_is_valid(token):
    try:
        expiry_str, signature = str(token).split(".", 1)
        expiry = int(expiry_str)
    except ValueError:
        return False
    return expiry > time.time() and hmac.compare_digest(signature, _sign(expiry))


def run_cookie_script(js):
    """Run a snippet in a zero-height iframe that has same-origin access to the
    app page, which is how a cookie is written or cleared from Streamlit."""
    html = f"<script>{js}</script>"
    if hasattr(st, "iframe"):  # newer Streamlit; components.html is deprecated there
        st.iframe(html, height=1)
    else:
        import streamlit.components.v1 as components

        components.html(html, height=0)


def set_auth_cookie():
    run_cookie_script(
        "const p = window.parent;"
        f"p.document.cookie = '{COOKIE_NAME}={make_token()}; max-age={REMEMBER_DAYS * 86400}; "
        "path=/; SameSite=Lax' + (p.location.protocol === 'https:' ? '; Secure' : '');"
    )


def clear_auth_cookie():
    run_cookie_script(
        f"window.parent.document.cookie = '{COOKIE_NAME}=; max-age=0; path=/; SameSite=Lax';"
    )


if "unlocked" not in st.session_state:
    st.session_state.unlocked = False
    st.session_state.logged_out = False  # ignore the browser's old cookie after an explicit lock

# Returning visitor (e.g. after a page refresh): the browser sends its cookies
# with the very first request, so a valid token unlocks the app immediately.
if COOKIE_SECRET and not st.session_state.unlocked and not st.session_state.logged_out:
    if token_is_valid(st.context.cookies.get(COOKIE_NAME, "")):
        st.session_state.unlocked = True

# Cookie writes happen on the run *after* the click that caused them, because
# the script is interrupted by st.rerun() before the browser could execute it.
pending_cookie = st.session_state.pop("pending_cookie", None)
if pending_cookie == "set":
    set_auth_cookie()
elif pending_cookie == "clear":
    clear_auth_cookie()

if not st.session_state.unlocked:
    st.title(APP_TITLE)
    st.markdown("🔒 This app is protected. Please request the passcode from the admin to continue.")
    with st.form("unlock_form"):
        entered = st.text_input("Passcode", type="password")
        remember = False
        if COOKIE_SECRET:
            remember = st.checkbox(f"Keep me signed in on this device ({REMEMBER_DAYS} days)", value=True)
        if st.form_submit_button("Unlock"):
            if hmac.compare_digest(entered.encode("utf-8"), str(PASSCODE).encode("utf-8")):
                st.session_state.unlocked = True
                st.session_state.logged_out = False
                if remember:
                    st.session_state.pending_cookie = "set"
                st.rerun()
            else:
                st.error("Incorrect passcode.")
    st.stop()

def lock_app():
    # Runs as a button callback, i.e. before the page reruns, which is the only
    # moment a widget's own state (the Manage data toggle) may be changed.
    st.session_state.unlocked = False
    st.session_state.logged_out = True
    st.session_state.pending_cookie = "clear"
    st.session_state.show_manage = False


# Header row: title on the left; the Manage data toggle and Lock button on the right.
header_title, header_toggle, header_lock = st.columns([5, 2, 1.6], vertical_alignment="center")
header_title.title(APP_TITLE)
show_manage = header_toggle.toggle(
    "⚙️ Manage data", key="show_manage", help="Show the panel to add, edit or delete people."
)
header_lock.button("🔒 Lock app", on_click=lock_app, use_container_width=True)

st.markdown(
    """
    Data is loaded live and can be edited directly from this app (add, update, or remove people).
    """
)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]
DEFAULT_SHEET_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1GGMarbxQ7XNC5qcUO_3iIWdcGMP27V4OETGRaWEBMgk/edit?gid=0#gid=0"
)
REQUIRED_COLS = [
    "ID", "Name", "Age", "ParentID",
    "Gender", "EmploymentStatus", "City", "MaritalStatus", "Spouse",
]
GENDER_OPTIONS = ["Male", "Female"]
EMPLOYMENT_OPTIONS = ["Student", "Self-employed", "Employed", "Unemployed"]
MARITAL_OPTIONS = ["Not yet married", "Married", "Divorced", "Widowed"]


def parse_sheet_url(url):
    """Extract the file ID and gid (tab) from a Google Sheets URL."""
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url)
    if not match:
        return None, None
    file_id = match.group(1)
    gid_match = re.search(r"[?#&]gid=(\d+)", url)
    gid = gid_match.group(1) if gid_match else "0"
    return file_id, gid


@st.cache_resource(show_spinner=False)
def get_client():
    """Authorize a gspread client from the service account in st.secrets.
    Cached so we don't re-authenticate on every rerun."""
    service_account_info = dict(st.secrets["connections"]["gsheets"])
    # The connections.gsheets block includes a "type" key used by some
    # connectors; Credentials only wants the service-account fields.
    service_account_info.pop("type", None)
    creds = Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
    return gspread.authorize(creds)


def get_worksheet(file_id, gid):
    client = get_client()
    spreadsheet = client.open_by_key(file_id)
    worksheet = None
    for ws in spreadsheet.worksheets():
        if str(ws.id) == gid:
            worksheet = ws
            break
    if worksheet is None:
        worksheet = spreadsheet.sheet1
    return worksheet


def load_dataframe(worksheet):
    records = worksheet.get_all_records()
    df = pd.DataFrame(records)
    return df


def get_header(worksheet):
    header = worksheet.row_values(1)
    return header if header else REQUIRED_COLS


def row_number_for_id(worksheet, header, person_id):
    """Return the 1-indexed sheet row number for a given ID (or None)."""
    id_col_index = header.index("ID") + 1  # 1-indexed for gspread
    id_values = worksheet.col_values(id_col_index)[1:]  # skip header
    for i, val in enumerate(id_values, start=2):  # sheet rows start at 2
        if str(val) == str(person_id):
            return i
    return None


def to_native(val):
    """Convert numpy/pandas scalar types (e.g. numpy.int64 pulled out of a
    DataFrame) into plain Python types so they can be JSON-serialized by
    gspread. Leaves None and ordinary Python values untouched."""
    if val is None:
        return None
    if pd.isna(val):
        return ""
    if isinstance(val, (np.integer,)):
        return int(val)
    if isinstance(val, (np.floating,)):
        return float(val)
    if isinstance(val, (np.bool_,)):
        return bool(val)
    return val


def add_person(worksheet, header, id_val, name, age, parent_id, gender,
               employment_status, city, marital_status, spouse):
    row_dict = {
        "ID": id_val,
        "Name": name,
        "Age": age,
        "ParentID": parent_id,
        "Gender": gender,
        "EmploymentStatus": employment_status,
        "City": city,
        "MaritalStatus": marital_status,
        "Spouse": spouse,
    }
    row = [to_native(row_dict.get(col, "")) for col in header]
    worksheet.append_row(row, value_input_option="USER_ENTERED")


def update_person(worksheet, header, row_num, name, age, parent_id, gender,
                   employment_status, city, marital_status, spouse):
    row_dict = {
        "Name": name,
        "Age": age,
        "ParentID": parent_id,
        "Gender": gender,
        "EmploymentStatus": employment_status,
        "City": city,
        "MaritalStatus": marital_status,
        "Spouse": spouse,
    }
    for col, val in row_dict.items():
        if col in header:
            col_index = header.index(col) + 1
            worksheet.update_cell(row_num, col_index, to_native(val))


def delete_person(worksheet, row_num):
    worksheet.delete_rows(row_num)


# ---------------------------------------------------------------------------
# Sidebar: connect to the private Google Sheet
# ---------------------------------------------------------------------------
df = None
worksheet = None
header = REQUIRED_COLS

if "connections" not in st.secrets or "gsheets" not in st.secrets["connections"]:
    st.error(
        "No service account found in st.secrets['connections']['gsheets']. "
        "Add one to connect to your private Google Sheet."
    )
    st.stop()

sheet_url = st.secrets["connections"]["gsheets"].get("spreadsheet", DEFAULT_SHEET_URL)
file_id, gid = parse_sheet_url(sheet_url)
if not file_id:
    st.error("Couldn't parse a Google Sheet ID from that URL.")
    st.stop()

try:
    worksheet = get_worksheet(file_id, gid)
    df = load_dataframe(worksheet)
    header = get_header(worksheet)
except Exception as e:
    st.error(
        "Couldn't load that sheet. Make sure it's shared with your service "
        f"account's email with at least Editor access. Error: {e}"
    )
    st.stop()

# ---------------------------------------------------------------------------
# Validate required columns
# ---------------------------------------------------------------------------
if not set(REQUIRED_COLS).issubset(df.columns):
    st.error(f"Your sheet must contain these columns: {REQUIRED_COLS}")
    st.stop()

# Normalize ParentID: blanks / 0 / -1 all mean "no parent"
def normalize_parent(val):
    if pd.isna(val) or val == "" or val in (0, -1, "0", "-1"):
        return None
    return val

df["ParentID"] = df["ParentID"].apply(normalize_parent)

for col in ["Gender", "EmploymentStatus", "City", "MaritalStatus", "Spouse"]:
    if col not in df.columns:
        df[col] = ""
    df[col] = df[col].fillna("").astype(str).str.strip()
df.loc[df["MaritalStatus"] == "", "MaritalStatus"] = "Not yet married"


def marital_clause(marital_status, spouse):
    if marital_status == "Married":
        return f"Married to {spouse}" if spouse else "Married"
    return marital_status if marital_status else "Not yet married"

# A "root" is anyone with no ParentID. We cap this at two so the tree always
# has a single top-level couple (or one root ancestor) instead of sprawling
# into multiple unrelated trees.
ROOT_LIMIT = 2


def current_root_count(dframe):
    return int(dframe["ParentID"].isna().sum())

# ---------------------------------------------------------------------------
# Page layout: the tree on the left and, only while the "Manage data" toggle is
# on, a panel on the right for adding, editing and deleting people.
# ---------------------------------------------------------------------------
if show_manage:
    main_col, side_col = st.columns([5, 2], gap="large")
else:
    main_col = st.container()
    side_col = None

if show_manage:
    with side_col:
        with st.container(border=True):
            name_options = df["Name"].tolist()

            st.subheader("Manage Data")
            add_tab, edit_tab, delete_tab = st.tabs(["➕ Add", "✏️ Edit", "🗑️ Delete"])

            with add_tab:
                with st.form("add_person_form", clear_on_submit=True):
                    st.write("Add a new person")
                    next_id = int(df["ID"].max()) + 1 if not df.empty else 1
                    st.caption(f"ID will be auto-assigned: **{next_id}**")
                    new_name = st.text_input("Name")
                    new_age = st.number_input("Age", min_value=0, max_value=130, value=30, step=1)
                    root_count = current_root_count(df)
                    add_parent_options = name_options.copy()
                    if root_count < ROOT_LIMIT:
                        add_parent_options = ["None (root)"] + add_parent_options
                    else:
                        st.caption(
                            f"Root limit reached ({ROOT_LIMIT} root ancestors already set) — "
                            "new people must be assigned a parent."
                        )
                    new_parent = st.selectbox("Parent", options=add_parent_options, key="add_parent")
                    new_gender = st.selectbox("Gender", options=GENDER_OPTIONS, key="add_gender")
                    new_employment = st.selectbox("Employment status", options=EMPLOYMENT_OPTIONS, key="add_employment")
                    new_city = st.text_input("City", key="add_city")
                    new_marital = st.selectbox("Marital status", options=MARITAL_OPTIONS, key="add_marital")
                    new_spouse = st.text_input("Spouse's name (if married)", key="add_spouse")
                    submitted = st.form_submit_button("Add person")
                    if submitted:
                        if not new_name.strip():
                            st.warning("Name is required.")
                        elif new_parent == "None (root)" and current_root_count(df) >= ROOT_LIMIT:
                            # Re-check at submit time in case the sheet changed since the
                            # form was rendered (e.g. someone else added a root in between).
                            st.error(f"Only {ROOT_LIMIT} root ancestors are allowed. Please choose a parent.")
                        else:
                            # Re-derive the next ID at submit time in case the sheet changed
                            # since the form was rendered.
                            new_id = int(df["ID"].max()) + 1 if not df.empty else 1
                            parent_id = None
                            if new_parent != "None (root)":
                                parent_id = df.loc[df["Name"] == new_parent, "ID"].iloc[0]
                            spouse_val = new_spouse.strip() if new_marital == "Married" else ""
                            try:
                                add_person(
                                    worksheet, header, new_id, new_name.strip(), new_age, parent_id,
                                    new_gender, new_employment, new_city.strip(), new_marital, spouse_val,
                                )
                                st.success(f"Added {new_name}.")
                                st.rerun()
                            except Exception as e:
                                st.error(f"Couldn't add person: {e}")

            with edit_tab:
                if df.empty:
                    st.write("No people to edit yet.")
                else:
                    edit_name = st.selectbox("Select a person to edit", options=df["Name"].tolist(), key="edit_select")
                    edit_row = df[df["Name"] == edit_name].iloc[0]
                    with st.form("edit_person_form"):
                        edited_name = st.text_input("Name", value=edit_row["Name"])
                        edited_age = st.number_input(
                            "Age", min_value=0, max_value=130,
                            value=int(edit_row["Age"]) if pd.notna(edit_row["Age"]) else 0, step=1,
                        )
                        current_parent_name = "None (root)"
                        is_currently_root = pd.isna(edit_row["ParentID"])
                        if not is_currently_root:
                            match = df[df["ID"] == edit_row["ParentID"]]
                            if not match.empty:
                                current_parent_name = match.iloc[0]["Name"]
                        allow_root_option = is_currently_root or (current_root_count(df) < ROOT_LIMIT)
                        parent_choices = [n for n in name_options if n != edit_name]
                        if allow_root_option:
                            parent_choices = ["None (root)"] + parent_choices
                        elif current_parent_name == "None (root)":
                            # Shouldn't happen (is_currently_root already covers this), but
                            # guard against an inconsistent default_index lookup below.
                            current_parent_name = parent_choices[0] if parent_choices else "None (root)"
                        default_index = parent_choices.index(current_parent_name) if current_parent_name in parent_choices else 0
                        edited_parent = st.selectbox("Parent", options=parent_choices, index=default_index, key="edit_parent")
                        if not allow_root_option:
                            st.caption(
                                f"Root limit reached ({ROOT_LIMIT} root ancestors already set) — "
                                "this person must have a parent."
                            )

                        gender_index = GENDER_OPTIONS.index(edit_row["Gender"]) if edit_row["Gender"] in GENDER_OPTIONS else 0
                        edited_gender = st.selectbox("Gender", options=GENDER_OPTIONS, index=gender_index, key="edit_gender")

                        employment_index = (
                            EMPLOYMENT_OPTIONS.index(edit_row["EmploymentStatus"])
                            if edit_row["EmploymentStatus"] in EMPLOYMENT_OPTIONS else 0
                        )
                        edited_employment = st.selectbox(
                            "Employment status", options=EMPLOYMENT_OPTIONS, index=employment_index, key="edit_employment"
                        )

                        edited_city = st.text_input("City", value=edit_row["City"], key="edit_city")

                        marital_index = (
                            MARITAL_OPTIONS.index(edit_row["MaritalStatus"])
                            if edit_row["MaritalStatus"] in MARITAL_OPTIONS else 0
                        )
                        edited_marital = st.selectbox("Marital status", options=MARITAL_OPTIONS, index=marital_index, key="edit_marital")
                        edited_spouse = st.text_input("Spouse's name (if married)", value=edit_row["Spouse"], key="edit_spouse")

                        save = st.form_submit_button("Save changes")
                        if save:
                            would_be_new_root = edited_parent == "None (root)" and not is_currently_root
                            if would_be_new_root and current_root_count(df) >= ROOT_LIMIT:
                                st.error(f"Only {ROOT_LIMIT} root ancestors are allowed. Please choose a parent.")
                            else:
                                parent_id = None
                                if edited_parent != "None (root)":
                                    parent_id = df.loc[df["Name"] == edited_parent, "ID"].iloc[0]
                                spouse_val = edited_spouse.strip() if edited_marital == "Married" else ""
                                row_num = row_number_for_id(worksheet, header, edit_row["ID"])
                                if row_num is None:
                                    st.error("Couldn't locate that row in the sheet.")
                                else:
                                    try:
                                        update_person(
                                            worksheet, header, row_num, edited_name.strip(), edited_age, parent_id,
                                            edited_gender, edited_employment, edited_city.strip(), edited_marital, spouse_val,
                                        )
                                        st.success(f"Updated {edited_name}.")
                                        st.rerun()
                                    except Exception as e:
                                        st.error(f"Couldn't update person: {e}")

            with delete_tab:
                deletable_df = df[df["ParentID"].notna()]
                if df.empty:
                    st.write("No people to delete yet.")
                elif deletable_df.empty:
                    st.write(
                        "Only root ancestors remain — they can't be deleted, only edited."
                    )
                else:
                    st.caption("Root ancestors can't be deleted here — edit them instead.")
                    delete_name = st.selectbox(
                        "Select a person to delete", options=deletable_df["Name"].tolist(), key="delete_select"
                    )
                    delete_row = deletable_df[deletable_df["Name"] == delete_name].iloc[0]
                    has_children = not df[df["ParentID"] == delete_row["ID"]].empty
                    if has_children:
                        st.warning(
                            "This person has children in the tree. Deleting them will leave "
                            "those children without a parent (they'll become roots)."
                        )
                    if st.button("Delete person", type="primary"):
                        if pd.isna(delete_row["ParentID"]):
                            # Shouldn't be reachable since roots are excluded from the
                            # dropdown above, but guard against a stale selection anyway.
                            st.error("Root ancestors can't be deleted — edit them instead.")
                        else:
                            row_num = row_number_for_id(worksheet, header, delete_row["ID"])
                            if row_num is None:
                                st.error("Couldn't locate that row in the sheet.")
                            else:
                                try:
                                    delete_person(worksheet, row_num)
                                    st.success(f"Deleted {delete_name}.")
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Couldn't delete person: {e}")

# ---------------------------------------------------------------------------
# Build the tree recursively
# ---------------------------------------------------------------------------
main_col.subheader("Family Tree")

valid_ids = set(df["ID"])

# Build a lookup: parent_id -> list of child rows (as dicts), for O(1) access
# instead of re-filtering the dataframe at every recursive call.
children_lookup = {}
for _, row in df.iterrows():
    parent_id = row["ParentID"]
    if pd.notna(parent_id) and parent_id in valid_ids:
        children_lookup.setdefault(parent_id, []).append(row)

# Roots = anyone whose ParentID is missing/blank, or points to an ID not in the data
root_rows = [
    row for _, row in df.iterrows()
    if pd.isna(row["ParentID"]) or row["ParentID"] not in valid_ids
]

# If there are exactly two root ancestors, treat them as married to each other
# automatically — no need to fill in MaritalStatus/Spouse for them by hand.
auto_marital = {}
if len(root_rows) == ROOT_LIMIT:
    r1, r2 = root_rows
    auto_marital[r1["ID"]] = ("Married", r2["Name"])
    auto_marital[r2["ID"]] = ("Married", r1["Name"])


def get_marital(row):
    if row["ID"] in auto_marital:
        return auto_marital[row["ID"]]
    return row["MaritalStatus"], row["Spouse"]


def render_person(row, depth=0, visited=None):
    """Recursively render a person and their descendants as nested,
    indented entries. `visited` guards against cyclic ParentID data."""
    if visited is None:
        visited = set()

    if row["ID"] in visited:
        st.error(f"Cycle detected involving ID {row['ID']} ({row['Name']}) — stopping recursion here.")
        return
    visited = visited | {row["ID"]}

    child_rows = children_lookup.get(row["ID"], [])
    indent = "&nbsp;" * 20 * depth
    branch = "└─ " if depth > 0 else ""
    age_display = int(row["Age"]) if pd.notna(row["Age"]) else "N/A"

    details = ", ".join(part for part in [row["Gender"], row["EmploymentStatus"], row["City"]] if part)
    status, spouse = get_marital(row)
    marital = marital_clause(status, spouse)

    label = f"{indent}{branch}**{row['Name']}** (Age: {age_display})"
    if details:
        label += f" - {details}, ({marital})"
    else:
        label += f" ({marital})"

    if child_rows:
        st.markdown(label, unsafe_allow_html=True)
        for child in sorted(child_rows, key=lambda r: r["Name"]):
            render_person(child, depth=depth + 1, visited=visited)
    else:
        st.markdown(label, unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Top-to-bottom family chart (pure HTML/CSS, no extra libraries)
# ---------------------------------------------------------------------------
TREE_CSS = """
<style>
.ft-scroll{overflow-x:auto;padding:4px 0 10px}
.ft-tree{--ft-line:rgba(128,128,128,.7);width:max-content;min-width:100%;display:flex;justify-content:center}
.ft-tree ul{display:flex;justify-content:center;margin:0;padding:20px 0 0;position:relative}
.ft-tree > ul{padding-top:0}
.ft-tree li{list-style:none;position:relative;padding:20px 6px 0;text-align:center}
.ft-tree li:only-child{padding-top:0}
.ft-tree li::before,.ft-tree li::after{content:"";position:absolute;top:0;right:50%;width:50%;height:20px;border-top:2px solid var(--ft-line)}
.ft-tree li::after{right:auto;left:50%;border-left:2px solid var(--ft-line)}
.ft-tree li:only-child::before,.ft-tree li:only-child::after{display:none}
.ft-tree li:first-child::before,.ft-tree li:last-child::after{border:0 none}
.ft-tree li:last-child::before{border-right:2px solid var(--ft-line);border-radius:0 6px 0 0}
.ft-tree li:first-child::after{border-radius:6px 0 0 0}
.ft-tree ul ul::before{content:"";position:absolute;top:0;left:50%;height:20px;border-left:2px solid var(--ft-line)}
.ft-unit{display:flex;justify-content:center;align-items:center}
.ft-card{min-width:190px;max-width:290px;padding:8px 12px;overflow-wrap:anywhere;border-radius:10px;border:1px solid rgba(128,128,128,.4);border-top:4px solid #8a8f98;background:rgba(128,128,128,.10);font-size:.82rem;line-height:1.35;text-align:center}
.ft-male{border-top-color:#4c8dff}
.ft-female{border-top-color:#ec6fa5}
.ft-other{border-top-color:#8a8f98}
.ft-name{font-weight:700;font-size:.95rem}
.ft-meta{opacity:.8}
.ft-marital{margin-top:4px;font-size:.76rem;font-style:italic;opacity:.9}
.ft-link{padding:0 8px;color:#e0556b;font-size:1.1rem}
</style>
"""


def person_card(row):
    """One person as an HTML card. Every value from the sheet is escaped."""
    status, spouse = get_marital(row)
    age = int(row["Age"]) if pd.notna(row["Age"]) else "N/A"
    gender = row["Gender"]
    css = {"Male": "ft-male", "Female": "ft-female"}.get(gender, "ft-other")
    meta = " · ".join(p for p in [f"Age {age}", gender] if p)
    work = " · ".join(p for p in [row["EmploymentStatus"], row["City"]] if p)
    parts = [
        f'<div class="ft-name">{esc(str(row["Name"]))}</div>',
        f'<div class="ft-meta">{esc(meta)}</div>',
    ]
    if work:
        parts.append(f'<div class="ft-meta">{esc(work)}</div>')
    parts.append(f'<div class="ft-marital">{esc(marital_clause(status, spouse))}</div>')
    return f'<div class="ft-card {css}">{"".join(parts)}</div>'


def tree_node(people, visited):
    """<li> for one person, or for a married couple shown side by side, with
    all of their children nested below. `visited` guards against cycles."""
    ids = {p["ID"] for p in people}
    if ids & visited:
        return '<li><div class="ft-unit"><div class="ft-card ft-other">⚠ Cycle detected in ParentID data</div></div></li>'
    visited = visited | ids

    kids = sorted(
        (k for p in people for k in children_lookup.get(p["ID"], [])),
        key=lambda r: str(r["Name"]),
    )
    unit = '<div class="ft-link">♥</div>'.join(person_card(p) for p in people)
    out = f'<li><div class="ft-unit">{unit}</div>'
    if kids:
        out += "<ul>" + "".join(tree_node([k], visited) for k in kids) + "</ul>"
    return out + "</li>"


def render_tree_diagram(units):
    for unit in units:
        with st.container(border=True):
            st.html(
                TREE_CSS
                + f'<div class="ft-scroll"><div class="ft-tree"><ul>{tree_node(unit, set())}</ul></div></div>'
            )


with main_col:
    if not root_rows:
        st.warning("No root ancestors found — check that ParentID values are either blank or reference valid IDs.")
    else:
        tree_view = st.radio(
            "Tree view",
            ["Family chart", "Indented list"],
            horizontal=True,
            label_visibility="collapsed",
        )
        sorted_roots = sorted(root_rows, key=lambda r: r["Name"])

        if tree_view == "Family chart":
            st.caption("Large family? Scroll sideways inside the chart to see every branch.")
            # The two root ancestors are a couple: draw them side by side at the
            # top, with the children of either one hanging below them.
            if len(sorted_roots) == ROOT_LIMIT:
                render_tree_diagram([sorted_roots])
            else:
                render_tree_diagram([[r] for r in sorted_roots])
        else:
            for root in sorted_roots:
                with st.container(border=True):
                    render_person(root)

    st.divider()
    st.subheader("Tabular Data")
    st.dataframe(df, use_container_width=True)
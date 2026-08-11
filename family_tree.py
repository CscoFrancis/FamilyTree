import re
import numpy as np
import pandas as pd
import streamlit as st
import gspread
from google.oauth2.service_account import Credentials

st.set_page_config(page_title="Family Tree Viewer", layout="wide")

st.title("🌳 Family Tree Viewer")

st.markdown(
    """
    Data is loaded live and can be edited directly from this app (add, update, or remove people). 
    
    To perform these tasks, please request the passcode from the admin 

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
# Sidebar: CRUD — manage people
# ---------------------------------------------------------------------------
st.sidebar.divider()
st.sidebar.header("Manage Data")

EDIT_PASSCODE = st.secrets.get("auth", {}).get("passcode", None)

if "editing_unlocked" not in st.session_state:
    st.session_state.editing_unlocked = False

if EDIT_PASSCODE is None:
    st.sidebar.warning(
        "No passcode configured (add `[auth] passcode = \"...\"` to secrets.toml) — "
        "editing is open to everyone."
    )
    st.session_state.editing_unlocked = True
elif not st.session_state.editing_unlocked:
    entered = st.sidebar.text_input("Enter passcode to edit", type="password", key="passcode_input")
    if st.sidebar.button("Unlock editing"):
        if entered == EDIT_PASSCODE:
            st.session_state.editing_unlocked = True
            st.rerun()
        else:
            st.sidebar.error("Incorrect passcode.")

if not st.session_state.editing_unlocked:
    st.sidebar.info("Viewing is open to everyone. Enter the passcode above to add, edit, or delete people.")
else:
    if EDIT_PASSCODE is not None:
        st.sidebar.success("Editing unlocked.")
        if st.sidebar.button("Lock editing"):
            st.session_state.editing_unlocked = False
            st.rerun()

    name_options = df["Name"].tolist()

    add_tab, edit_tab, delete_tab = st.sidebar.tabs(["➕ Add", "✏️ Edit", "🗑️ Delete"])

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
        if df.empty:
            st.write("No people to delete yet.")
        else:
            delete_name = st.selectbox("Select a person to delete", options=df["Name"].tolist(), key="delete_select")
            delete_row = df[df["Name"] == delete_name].iloc[0]
            has_children = not df[df["ParentID"] == delete_row["ID"]].empty
            if has_children:
                st.warning(
                    "This person has children in the tree. Deleting them will leave "
                    "those children without a parent (they'll become roots)."
                )
            if st.button("Delete person", type="primary"):
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
st.subheader("Family Tree")

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


if not root_rows:
    st.warning("No root ancestors found — check that ParentID values are either blank or reference valid IDs.")
else:
    for root in sorted(root_rows, key=lambda r: r["Name"]):
        with st.container(border=True):
            render_person(root)

# ---------------------------------------------------------------------------
# Optional: person detail lookup
# ---------------------------------------------------------------------------
st.divider()
st.subheader("Look Up a Person")

if df.empty:
    st.write("No people in the sheet yet — add someone using the sidebar.")
else:
    selected_name = st.selectbox("Select a person", options=df["Name"].sort_values())
    person = df[df["Name"] == selected_name].iloc[0]

    parent_name = None
    if pd.notna(person["ParentID"]) and person["ParentID"] in valid_ids:
        parent_row = df[df["ID"] == person["ParentID"]]
        if not parent_row.empty:
            parent_name = parent_row.iloc[0]["Name"]

    children = df[df["ParentID"] == person["ID"]]["Name"].tolist()

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Name", person["Name"])
    col2.metric("Age", int(person["Age"]) if pd.notna(person["Age"]) else "N/A")
    col3.metric("Gender", person["Gender"] or "N/A")
    col4.metric("Parent", parent_name if parent_name else "None (Root)")

    col5, col6, col7 = st.columns(3)
    col5.metric("Employment", person["EmploymentStatus"] or "N/A")
    col6.metric("City", person["City"] or "N/A")
    person_status, person_spouse = get_marital(person)
    col7.metric("Marital status", marital_clause(person_status, person_spouse))

    if children:
        st.write("**Children:**", ", ".join(children))
    else:
        st.write("**Children:** None")

# ---------------------------------------------------------------------------
st.divider()
st.subheader("Tabular Data")
st.dataframe(df, use_container_width=True)
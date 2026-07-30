import re
import streamlit as st
import pandas as pd

st.set_page_config(page_title="Family Tree Viewer", layout="wide")

st.title("🌳 Family Tree Viewer")

st.markdown(
    """
    Load your data from a Google Sheet, upload a CSV/Excel file, or use the sample data.
    Your data should have the columns:
    **ID, Name, Age, ParentID**

    - `ID`: unique identifier for each person
    - `Name`: person's name
    - `Age`: person's age
    - `ParentID`: the `ID` of this person's parent (leave blank / use 0 or -1 for root ancestors)
    """
)

# ---------------------------------------------------------------------------
# Sample data (used if no file is uploaded)
# ---------------------------------------------------------------------------
sample_data = pd.DataFrame(
    [
        {"ID": 1, "Name": "George", "Age": 78, "ParentID": None},
        {"ID": 2, "Name": "Martha", "Age": 75, "ParentID": None},
        {"ID": 3, "Name": "John", "Age": 50, "ParentID": 1},
        {"ID": 4, "Name": "Anna", "Age": 48, "ParentID": 1},
        {"ID": 5, "Name": "Emma", "Age": 25, "ParentID": 3},
        {"ID": 6, "Name": "Liam", "Age": 22, "ParentID": 3},
        {"ID": 7, "Name": "Noah", "Age": 20, "ParentID": 4},
    ]
)

# ---------------------------------------------------------------------------
# Sidebar: data input
# ---------------------------------------------------------------------------
st.sidebar.header("Data Input")

data_source = st.sidebar.radio(
    "Choose data source", options=["Google Sheet", "Upload CSV/Excel", "Sample data"]
)

df = None

if data_source == "Google Sheet":
    st.sidebar.markdown(
        "Paste a Google Sheets URL. The sheet must be shared as "
        "**\"Anyone with the link can view\"**."
    )
    sheet_url = st.sidebar.text_input("Google Sheet URL")

    if sheet_url:
        try:
            # Extract the file ID and, if present, the gid (specific tab)
            # from a typical Google Sheets URL, e.g.:
            # https://docs.google.com/spreadsheets/d/<FILE_ID>/edit#gid=<GID>
            match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", sheet_url)
            if not match:
                st.sidebar.error("Couldn't parse a Google Sheet ID from that URL.")
            else:
                file_id = match.group(1)
                gid_match = re.search(r"[?#&]gid=(\d+)", sheet_url)
                gid = gid_match.group(1) if gid_match else "0"

                csv_url = (
                    f"https://docs.google.com/spreadsheets/d/{file_id}"
                    f"/export?format=csv&gid={gid}"
                )
                df = pd.read_csv(csv_url)
        except Exception as e:
            st.sidebar.error(
                "Couldn't load that sheet. Make sure it's shared as "
                f"'Anyone with the link can view'. Error: {e}"
            )

    if df is None:
        st.sidebar.info("No sheet loaded yet — using sample data.")
        df = sample_data.copy()

elif data_source == "Upload CSV/Excel":
    uploaded_file = st.sidebar.file_uploader("Upload CSV or Excel file", type=["csv", "xlsx"])
    if uploaded_file is not None:
        if uploaded_file.name.endswith(".csv"):
            df = pd.read_csv(uploaded_file)
        else:
            df = pd.read_excel(uploaded_file)
    else:
        st.sidebar.info("No file uploaded — using sample data.")
        df = sample_data.copy()

else:
    df = sample_data.copy()

# ---------------------------------------------------------------------------
# Validate required columns
# ---------------------------------------------------------------------------
required_cols = {"ID", "Name", "Age", "ParentID"}
if not required_cols.issubset(df.columns):
    st.error(f"Your data must contain these columns: {required_cols}")
    st.stop()

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

    label = f"{indent}{branch}**{row['Name']}** (Age: {age_display})"

    if child_rows:
        # Expander lets the user collapse large branches; nested expanders
        # aren't supported by Streamlit, so we use indented markdown instead
        # and only wrap the top level in a container for visual grouping.
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
selected_name = st.selectbox("Select a person", options=df["Name"].sort_values())
person = df[df["Name"] == selected_name].iloc[0]

parent_name = None
if pd.notna(person["ParentID"]) and person["ParentID"] in valid_ids:
    parent_row = df[df["ID"] == person["ParentID"]]
    if not parent_row.empty:
        parent_name = parent_row.iloc[0]["Name"]

children = df[df["ParentID"] == person["ID"]]["Name"].tolist()

col1, col2, col3 = st.columns(3)
col1.metric("Name", person["Name"])
col2.metric("Age", int(person["Age"]) if pd.notna(person["Age"]) else "N/A")
col3.metric("Parent", parent_name if parent_name else "None (Root)")

if children:
    st.write("**Children:**", ", ".join(children))
else:
    st.write("**Children:** None")
# -------------------------------------------------------------------------------
st.divider()
st.subheader("Raw Data")
st.dataframe(df, use_container_width=True)
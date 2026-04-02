"""
app.py — SPINS Measure Builder
Browse measures from measure_table, click to insert into the formula box,
then generate Whiz JSON steps. No LLM required.
"""

import json
import re
import sys
from io import StringIO
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent
BEEP_BOP_PATH = ROOT / "beep bop.xlsx"
sys.path.insert(0, str(ROOT / "tools"))

from formula_parser import formula_to_steps  # noqa: E402

st.set_page_config(page_title="SPINS Measure Builder", page_icon="🔢", layout="wide")


# ── Data loading ───────────────────────────────────────────────────────────────

@st.cache_data(show_spinner="Loading data…")
def load_data() -> list[dict]:
    import openpyxl
    wb = openpyxl.load_workbook(BEEP_BOP_PATH)
    ws = wb["measure_table"]
    rows = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        bucket1, bucket2, sys_desc, entity, short_name, alias, metric_type, agg, clean, script, fmt = row
        if not entity:
            continue
        entity_s = str(entity).strip()
        if entity_s == "To be added to Whiz":
            continue
        rows.append({
            "bucket1":     str(bucket1 or "").strip(),
            "bucket2":     str(bucket2 or "").strip(),
            "description": str(sys_desc or "").strip(),
            "entity":      entity_s,
            "short_name":  str(short_name or "").strip(),
            "metric_type": str(metric_type or "").strip(),
            "agg":         str(agg or "-").strip(),
            "clean":       str(clean or "-").strip(),
            "script":      str(script or "-").strip(),
        })
    return rows


# ── Helpers ────────────────────────────────────────────────────────────────────

AGG_MAP = {"Sum": "SUM", "Max": "MAX", "Min": "MAX"}

# ── Format builder ─────────────────────────────────────────────────────────────

FORMAT_TYPES = [
    "Currency – auto M/B scale",
    "Number – auto M/B scale",
    "Percentage",
    "Number",
    "Currency – plain (no scaling)",
    "Date",
    "Custom",
]

FORMAT_EXAMPLES = {
    "Currency – auto M/B scale":  "$1.2M  /  $4.5B  /  $999",
    "Number – auto M/B scale":    "1.2M  /  4.5B  /  999",
    "Percentage":                  "12.3%",
    "Number":                      "1,234.5",
    "Currency – plain (no scaling)": "$1,234.56",
    "Date":                        "03/26/26",
    "Custom":                      "(enter your own format string)",
}

def build_format_string(fmt_type: str, decimals: int, custom: str = "") -> str:
    """Return the Excel format string for the selected type and decimal places."""
    d = "0" * decimals
    dec = f".{d}" if decimals > 0 else ""

    if fmt_type == "Currency – auto M/B scale":
        return (
            f'[>=1000000000]$#,##0{dec},,,"B";'
            f'[>=1000000]$#,##0{dec},,"M";'
            f'$#,##0{dec}'
        )
    elif fmt_type == "Number – auto M/B scale":
        return (
            f'[>=1000000000]#,##0{dec},,,"B";'
            f'[>=1000000]#,##0{dec},,"M";'
            f'#,##0{dec}'
        )
    elif fmt_type == "Percentage":
        return f'#,##0{dec}\\%'
    elif fmt_type == "Number":
        return f'#,##0{dec}'
    elif fmt_type == "Currency – plain (no scaling)":
        return f'$#,##0{dec}'
    elif fmt_type == "Date":
        return "MM/DD/YY"
    elif fmt_type == "Custom":
        return custom.strip()
    return ""

def formula_fragment(m: dict) -> str | None:
    """
    Return the formula fragment to insert for a measure, or None if not insertable.
    - Base / Base & Script  → SUM(ENTITY) or MAX(ENTITY) based on Agg column
    - Calc with clean formula → (clean_formula) wrapped in parens
    - Calc without clean / Script → not insertable
    """
    if m["metric_type"] in ("Base", "Base & Script"):
        func = AGG_MAP.get(m["agg"])
        if func:
            return f"{func}({m['entity']})"
    elif m["metric_type"] == "Calc":
        clean = m["clean"].strip()
        if clean and clean != "-":
            return f"({clean})"
    return None


def do_insert(fragment: str) -> None:
    """Append a formula fragment to the draft, adding a space separator if needed."""
    current = st.session_state.get("formula_draft", "")
    if current and current[-1] not in "( ":
        current += " "
    st.session_state["formula_draft"] = current + fragment


# ── Session state ──────────────────────────────────────────────────────────────

for _k, _v in [
    ("measure_list", []),
    ("preview", None),
    ("formula_draft", ""),
    ("_add_warning", ""),
]:
    if _k not in st.session_state:
        st.session_state[_k] = _v


# ── Load ───────────────────────────────────────────────────────────────────────

all_measures = load_data()
bucket1_opts = ["All"] + sorted({m["bucket1"] for m in all_measures if m["bucket1"]})


# ── Sidebar: Measure Browser ───────────────────────────────────────────────────

with st.sidebar:
    st.markdown("### Measure Browser")
    st.caption("Click **＋** to insert a measure into the formula.")

    search = st.text_input(
        "Search", placeholder="Search entity or description…",
        label_visibility="collapsed", key="sidebar_search",
    )
    b1 = st.selectbox("Bucket 1", bucket1_opts, label_visibility="collapsed")
    b2_opts = (
        ["All"] + sorted({m["bucket2"] for m in all_measures
                          if m["bucket1"] == b1 and m["bucket2"]})
        if b1 != "All" else ["All"]
    )
    b2 = st.selectbox("Bucket 2", b2_opts, label_visibility="collapsed")

    # Apply filters
    filtered = all_measures
    if b1 != "All":
        filtered = [m for m in filtered if m["bucket1"] == b1]
    if b2 != "All":
        filtered = [m for m in filtered if m["bucket2"] == b2]
    if search.strip():
        q = search.strip().lower()
        filtered = [m for m in filtered
                    if q in m["entity"].lower() or q in m["description"].lower()]

    st.caption(f"{len(filtered)} of {len(all_measures)} measures")
    st.divider()

    shown = filtered[:120]
    for m in shown:
        frag = formula_fragment(m)
        c_label, c_btn = st.columns([5, 1])
        with c_label:
            # Type label + color cue
            mtype = m["metric_type"]
            type_color = {
                "Base": "#2e7d32",
                "Base & Script": "#1565c0",
                "Calc": "#e65100",
                "Script": "#6a1b9a",
            }.get(mtype, "#555")
            type_html = (
                f"<span style='font-size:0.7em;font-weight:600;"
                f"color:{type_color};text-transform:uppercase;'>{mtype}</span>"
            )

            agg_badge = f" `{m['agg']}`" if m["agg"] != "-" else ""

            # For Calc measures, show a preview of the clean formula
            if mtype == "Calc" and m["clean"] not in ("-", ""):
                clean_preview = m["clean"][:55] + ("…" if len(m["clean"]) > 55 else "")
                sub = f"<span style='font-size:0.72em;color:#777;font-style:italic'>{clean_preview}</span>"
            else:
                sub = f"<span style='font-size:0.8em;color:gray'>{m['description']}</span>"

            st.markdown(
                f"**`{m['entity']}`**{agg_badge} {type_html}  \n{sub}",
                unsafe_allow_html=True,
            )
        with c_btn:
            if frag:
                st.button(
                    "＋", key=f"ins_{m['entity']}",
                    help=f"Insert: {frag[:120]}",
                    on_click=do_insert, args=(frag,),
                )
            else:
                # Non-insertable (Script or Calc without clean formula)
                st.markdown(
                    "<span style='color:#ccc;font-size:1.1em' title='No clean formula available'>✕</span>",
                    unsafe_allow_html=True,
                )

    if len(filtered) > 120:
        st.caption("Showing first 120. Narrow your search to see more.")

    st.divider()
    if st.button("Reload data", use_container_width=True):
        st.cache_data.clear()
        st.rerun()


# ── Main layout ────────────────────────────────────────────────────────────────

st.title("SPINS Measure Builder")
col_build, col_list = st.columns([1, 1], gap="large")


# ── Left: Single / Batch tabs ─────────────────────────────────────────────────

with col_build:
    tab_single, tab_batch = st.tabs(["Single", "Batch"])

with tab_single:
    st.subheader("Build a Measure")

    measure_name = st.text_input(
        "Measure Name", placeholder="e.g. ARP % Change YAGO",
    )

    st.markdown("**Formula**")
    st.caption(
        "Click **＋** in the sidebar to insert base measures. "
        "Type operators (`/` `-` `*`) between them."
    )

    # text_area bound to formula_draft session state key
    st.text_area(
        "Formula input",
        placeholder=(
            "e.g.  SUM(DOLLARS) / SUM(UNITS)\n"
            "      ((SUM(DOLLARS)/SUM(UNITS)) / (SUM(DOLLARS_STLY)/SUM(UNITS_STLY))) - 1"
        ),
        height=110,
        key="formula_draft",
        label_visibility="collapsed",
    )

    btn_c1, btn_c2 = st.columns([1, 1])
    with btn_c2:
        def _clear_formula():
            st.session_state["formula_draft"] = ""
        st.button("⌫ Clear formula", use_container_width=True, on_click=_clear_formula)

    st.markdown("**Treat `/` as**")
    div_type = st.radio(
        "Division type",
        ["division", "percent"],
        horizontal=True,
        label_visibility="collapsed",
        help=(
            "**division** — per-unit ratios (ARP, $/HH)  \n"
            "**percent** — share / ratio measures (% stores, SPM)"
        ),
    )

    # ── Format builder ──────────────────────────────────────────────────────────
    st.markdown("**Format**")
    fmt_col1, fmt_col2 = st.columns([2, 1])
    with fmt_col1:
        fmt_type = st.selectbox(
            "Format type", FORMAT_TYPES,
            label_visibility="collapsed", key="fmt_type",
        )
    with fmt_col2:
        if fmt_type in ("Currency – auto M/B scale", "Number – auto M/B scale",
                        "Percentage", "Number", "Currency – plain (no scaling)"):
            decimals = st.selectbox(
                "Decimal places", [0, 1, 2],
                index=1, label_visibility="collapsed", key="fmt_decimals",
            )
        else:
            decimals = 0
            st.write("")

    if fmt_type == "Custom":
        custom_fmt = st.text_input(
            "Custom format string",
            placeholder='e.g.  #,##0.0   or   $#,##0.00',
            label_visibility="collapsed", key="fmt_custom",
        )
    else:
        custom_fmt = ""

    fmt_string = build_format_string(fmt_type, decimals, custom_fmt)
    if fmt_string:
        example = FORMAT_EXAMPLES.get(fmt_type, "")
        st.caption(
            f"Format string: `{fmt_string}`"
            + (f"   ·   Example: {example}" if example else "")
        )

    st.divider()

    formula_val = st.session_state.get("formula_draft", "").strip()
    ready = bool(measure_name.strip()) and bool(formula_val)

    if st.button("Generate ▶", type="primary", use_container_width=True, disabled=not ready):
        try:
            steps = formula_to_steps(formula_val, measure_name.strip(), div_type)
            st.session_state["preview"] = {
                "name":       measure_name.strip(),
                "steps":      steps,
                "clean":      formula_val,
                "json_str":   json.dumps(steps),
                "format_str": fmt_string,
            }
        except Exception as e:
            st.error(f"Parse error — {e}")
            st.session_state["preview"] = None

    # ── Preview ────────────────────────────────────────────────────────────────
    if st.session_state["preview"]:
        p = st.session_state["preview"]
        st.divider()
        st.markdown(f"**Preview — {p['name']}**")
        st.caption(
            f"Clean: `{p['clean']}`"
            + (f"   ·   Format: `{p['format_str']}`" if p.get("format_str") else "")
        )
        st.code(json.dumps(p["steps"], indent=2), language="json")

        if st.session_state.get("_add_warning"):
            st.warning(st.session_state.pop("_add_warning"))

        def _add_to_list():
            p = st.session_state["preview"]
            if not p:
                return
            existing_names = [m["name"] for m in st.session_state["measure_list"]]
            if p["name"] in existing_names:
                st.session_state["_add_warning"] = f"'{p['name']}' is already in the list."
            else:
                st.session_state["measure_list"].append({
                    "name":       p["name"],
                    "clean":      p["clean"],
                    "json_str":   p["json_str"],
                    "format_str": p.get("format_str", ""),
                })
                st.session_state["preview"] = None
                st.session_state["formula_draft"] = ""

        st.button("➕ Add to list", use_container_width=True, type="secondary",
                  on_click=_add_to_list)


# ── Batch tab ──────────────────────────────────────────────────────────────────

with tab_batch:
    st.subheader("Batch Process")
    st.caption(
        "Enter one measure per row — or upload a CSV with `Measure Name` and `Formula` columns. "
        "Click **Process All** to generate JSON for every row at once."
    )

    # Optional CSV upload to pre-populate the table
    uploaded = st.file_uploader(
        "Upload CSV (optional)", type="csv", label_visibility="collapsed",
        key="batch_upload",
    )
    if uploaded is not None:
        try:
            up_df = pd.read_csv(uploaded)
            # Normalise column names case-insensitively
            up_df.columns = [c.strip() for c in up_df.columns]
            col_map = {c.lower(): c for c in up_df.columns}
            name_col    = col_map.get("measure name", col_map.get("name", None))
            formula_col = col_map.get("formula", col_map.get("clean formula",
                          col_map.get("measure calculation (clean)", None)))
            if name_col and formula_col:
                st.session_state["batch_rows"] = [
                    {
                        "Measure Name": str(r[name_col]).strip(),
                        "Formula":      str(r[formula_col]).strip(),
                        "Treat / as":   "division",
                        "Format":       "Currency – auto M/B scale",
                        "Decimals":     1,
                    }
                    for _, r in up_df.iterrows()
                ]
                st.success(f"Loaded {len(st.session_state['batch_rows'])} rows from CSV.")
            else:
                st.error("CSV must have 'Measure Name' and 'Formula' columns.")
        except Exception as e:
            st.error(f"Could not read CSV: {e}")

    # Initialise blank table if nothing loaded yet
    if "batch_rows" not in st.session_state:
        st.session_state["batch_rows"] = [
            {"Measure Name": "", "Formula": "", "Treat / as": "division",
             "Format": "Currency – auto M/B scale", "Decimals": 1},
        ] * 3

    edited_batch = st.data_editor(
        pd.DataFrame(st.session_state["batch_rows"]),
        num_rows="dynamic",
        use_container_width=True,
        column_config={
            "Treat / as": st.column_config.SelectboxColumn(
                options=["division", "percent"], required=True,
            ),
            "Format": st.column_config.SelectboxColumn(
                options=FORMAT_TYPES, required=True,
            ),
            "Decimals": st.column_config.SelectboxColumn(
                options=[0, 1, 2], required=True,
            ),
        },
        key="batch_editor",
    )

    if st.button("⚙ Process All", type="primary", use_container_width=True):
        results = []
        for _, row in edited_batch.iterrows():
            name    = str(row.get("Measure Name", "")).strip()
            formula = str(row.get("Formula", "")).strip()
            if not name or not formula:
                continue
            div_t   = str(row.get("Treat / as", "division"))
            fmt_t   = str(row.get("Format", "Currency – auto M/B scale"))
            dec     = int(row.get("Decimals", 1))
            try:
                steps   = formula_to_steps(formula, name, div_t)
                fmt_str = build_format_string(fmt_t, dec)
                results.append({
                    "Measure Name":                name,
                    "Measure Calculation":         json.dumps(steps),
                    "Measure Calculation (Clean)": formula,
                    "Format":                      fmt_str,
                    "_ok": True,
                })
            except Exception as e:
                results.append({
                    "Measure Name": name,
                    "Measure Calculation": f"ERROR: {e}",
                    "Measure Calculation (Clean)": formula,
                    "Format": "",
                    "_ok": False,
                })
        st.session_state["batch_results"] = results

    if st.session_state.get("batch_results"):
        results = st.session_state["batch_results"]
        ok      = [r for r in results if r["_ok"]]
        errors  = [r for r in results if not r["_ok"]]

        st.caption(f"✓ {len(ok)} succeeded   {'  ✗ ' + str(len(errors)) + ' failed' if errors else ''}")

        # Status table
        st.dataframe(
            pd.DataFrame([
                {
                    "Measure Name": r["Measure Name"],
                    "Status": "✓ OK" if r["_ok"] else f"✗ {r['Measure Calculation']}",
                }
                for r in results
            ]),
            use_container_width=True, hide_index=True,
        )

        if ok:
            # Download JSON (strip internal _ok flag)
            output = [{k: v for k, v in r.items() if k != "_ok"} for r in ok]
            st.download_button(
                "⬇ Download JSON",
                data=json.dumps(output, indent=2),
                file_name="batch_measures.json",
                mime="application/json",
                use_container_width=True,
            )
            # Also offer CSV
            buf = StringIO()
            pd.DataFrame(output).to_csv(buf, index=False)
            st.download_button(
                "⬇ Download CSV",
                data=buf.getvalue(),
                file_name="batch_measures.csv",
                mime="text/csv",
                use_container_width=True,
            )


# ── Right: Measure List + Export ───────────────────────────────────────────────

with col_list:
    st.subheader("Measure List")

    if not st.session_state["measure_list"]:
        st.caption("No measures yet. Build and add measures on the left.")
    else:
        df_display = pd.DataFrame([
            {
                "Measure Name": m["name"],
                "Clean Formula": m["clean"],
                "Format": m.get("format_str", ""),
                "Measure Calculation (JSON)": m["json_str"],
            }
            for m in st.session_state["measure_list"]
        ])
        st.dataframe(df_display, use_container_width=True, hide_index=True)

        remove_name = st.selectbox(
            "Remove a measure",
            ["— Select to remove —"] + [m["name"] for m in st.session_state["measure_list"]],
        )
        if remove_name != "— Select to remove —":
            if st.button(f"Remove '{remove_name}'", use_container_width=True):
                st.session_state["measure_list"] = [
                    m for m in st.session_state["measure_list"] if m["name"] != remove_name
                ]
                st.rerun()

        st.divider()

        # Build CSV matching the beep bop sheet columns
        csv_rows = [
            {
                "Measure Name":                m["name"],
                "Measure Calculation":         m["json_str"],
                "Measure Calculation (Clean)": m["clean"],
                "Format":                      m.get("format_str", ""),
            }
            for m in st.session_state["measure_list"]
        ]
        buf = StringIO()
        pd.DataFrame(csv_rows).to_csv(buf, index=False)

        st.download_button(
            "⬇ Download CSV",
            data=buf.getvalue(),
            file_name="new_measures.csv",
            mime="text/csv",
            use_container_width=True,
        )

        if st.button("Clear list", use_container_width=True):
            st.session_state["measure_list"] = []
            st.rerun()

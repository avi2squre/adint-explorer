"""ADInt Explorer — interactive knowledge graph dashboard.

Renders the Zhang lab's ADInt Alzheimer's drug-repurposing knowledge graph with:
  • Zhang lab's 7-category color scheme (from neo4j_node_updated.csv)
  • Search-to-refocus on any concept
  • Click any node to refocus the view on that concept
  • Filter checkboxes per category
  • Shortest-path highlighting between any two concepts (via NetworkX)
  • Evidence panel with PMID + source sentence on edge click
"""

from __future__ import annotations

import math
from pathlib import Path

import dash
import dash_cytoscape as cyto
import networkx as nx
import pandas as pd
from dash import Input, Output, State, callback_context, dcc, html, no_update

# --------------
# Configuration
# --------------

DATA_DIR = Path(__file__).parent
ALZHEIMERS_CUI = "C0002395"
PER_CATEGORY_TOP_K = 7  # neighbors shown PER category around the focal node (all 7 categories appear)
SEARCH_DROPDOWN_LIMIT = 7000  # cap dropdowns 
# Zhang lab color convention.
CATEGORY_COLORS = {
    "Drug":                                  "#EC993F", 
    "Dietary Supplement":                    "#6FBF73",  
    "Complementary and Integrative Health":  "#2CA6A4",  
    "Disease or Syndrome":                   "#E5736A",  
    "Gene or Genome":                        "#4A7FD6",  
    "Organism Function":                     "#9B59B6",  
    "Other":                                 "#B0B0B0",  
}

# ----------
# Load data
# ----------

print("Loading ADIntKG.tsv ...")
edges_df = pd.read_csv(DATA_DIR / "ADIntKG.tsv", sep="\t")
print(f"  {len(edges_df):,} evidence rows (PMID-level)")

print("Loading Zhang lab node table (neo4j_node_updated.csv) ...")
nodes_df = pd.read_csv(DATA_DIR / "Neo4j_data" / "neo4j_node_updated.csv")
print(f"  {len(nodes_df):,} typed concepts")


def classify(category: str, semtype: str, label: str) -> str:
    """Resolve a concept to one of Zhang's 7 display categories.

    Rules are evaluated top-down — the first match wins. The Category column
    explicitly tags the dietary-supplement and CIH vocabularies; everything
    else falls through to UMLS Semantic_Type / Label inspection.
    """
    if category == "Complementary and Integrative Health":
        return "Complementary and Integrative Health"
    if category == "Dietary Supplement":
        return "Dietary Supplement"
    if semtype == "Disease or Syndrome":
        return "Disease or Syndrome"
    if semtype == "Gene or Genome":
        return "Gene or Genome"
    if semtype == "Organism Function":
        return "Organism Function"
    if label == "Chemicals & Drugs":
        return "Drug"
    return "Other"


nodes_df["display_category"] = [
    classify(c, s, l)
    for c, s, l in zip(nodes_df["Category"], nodes_df["Semantic_Type"], nodes_df["Label"])
]
cui_to_name = dict(zip(nodes_df["CUI"], nodes_df["Name"]))
cui_to_category = dict(zip(nodes_df["CUI"], nodes_df["display_category"]))
cui_to_semtype = dict(zip(nodes_df["CUI"], nodes_df["Semantic_Type"]))

# ---------------------------------------------------------------------------------------------
# Build NetworkX graph (one-time startup): for path-finding and degree-ranked neighbor lookups.
# ---------------------------------------------------------------------------------------------

print("Building NetworkX graph + directed predicate index ...")
# Dropping duplicate to one row per (S, P, O) directed triple
triples_df = edges_df[["SUBJECT_CUI", "PREDICATE", "OBJECT_CUI"]].drop_duplicates()
# Dropping self-loops
triples_df = triples_df[triples_df["SUBJECT_CUI"] != triples_df["OBJECT_CUI"]]
print(f"  {len(triples_df):,} unique directed triples")

# Undirected NetworkX graph for path-finding (direction doesn't matter for chain discovery)
G = nx.from_pandas_edgelist(
    triples_df, source="SUBJECT_CUI", target="OBJECT_CUI", create_using=nx.Graph
)
print(f"  {G.number_of_nodes():,} nodes / {G.number_of_edges():,} unique undirected edges")

# Directed predicate index. It preserves the original (S → O) direction of each predicate
# so the visualization can show arrows correctly (come back to this). Keyed by (subject, object).
from collections import defaultdict
edge_predicates: dict[tuple[str, str], list[str]] = defaultdict(list)
for s, p, o in zip(triples_df["SUBJECT_CUI"], triples_df["PREDICATE"], triples_df["OBJECT_CUI"]):
    edge_predicates[(s, o)].append(p)
print(f"  directed predicate index: {len(edge_predicates):,} (S, O) keys")

# Index evidence rows by (subject, predicate, object) for the evidence panel
evidence_index = edges_df.groupby(["SUBJECT_CUI", "PREDICATE", "OBJECT_CUI"])

# ---------
# Helpers
# ---------

def display_label(name: str, category: str) -> str:
    """Strip the noisy '|alias' suffix from UMLS preferred names.

    Many ADInt concept names embed multiple aliases separated by '|':
      - Genes: "EDN1 gene|EDN1"               → "EDN1"           (HGNC symbol)
      - Drugs: "Protein S100-A9|S100A9"       → "Protein S100-A9" (chemical name)
      - Other: pick the shortest non-empty token (usually the symbol/abbrev)

    The full name is preserved in cui_to_name for the search dropdown and
    evidence panel — only the on-graph label is shortened.
    """
    if not isinstance(name, str) or "|" not in name:
        return name
    parts = [p.strip() for p in name.split("|") if p.strip()]
    if not parts:
        return name
    if category == "Gene or Genome":
        return parts[-1]   # HGNC symbol is the last token
    if category == "Drug":
        return parts[0]    # chemical name is the first token
    return min(parts, key=len)


def top_neighbors(cui: str, k: int) -> list[str]:
    """Return up to k neighbors of `cui`, ranked by degree (most connected first)."""
    if cui not in G:
        return []
    nbrs = sorted(G.neighbors(cui), key=lambda n: G.degree(n), reverse=True)
    return nbrs[:k]


def stratified_neighbors(focal_cui: str, k_per_category: int = PER_CATEGORY_TOP_K) -> list[str]:
    """Return [focal_cui] + top-k highest-degree neighbors 
    This guarantees CIH, Gene or Genome, Organism Function, etc. are represented
    in the default view even though their absolute degrees are lower than the
    dominant Drug/Disease/Other concepts. Matches ADInt's intervention-centric
    research narrative — the whole point is to surface CIH and supplement candidates.
    """
    if focal_cui not in G:
        return [focal_cui]
    by_cat: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for n in G.neighbors(focal_cui):
        cat = cui_to_category.get(n, "Other")
        by_cat[cat].append((n, G.degree(n)))
    selected = [focal_cui]
    for cat in CATEGORY_COLORS:  # iterate in declared order so colors are balanced
        nbrs = sorted(by_cat.get(cat, []), key=lambda x: x[1], reverse=True)
        selected.extend([n for n, _ in nbrs[:k_per_category]])
    return selected


def shortest_path(start: str, end: str) -> list[str]:
    """Shortest undirected path between two CUIs, or [] if none exists."""
    try:
        return nx.shortest_path(G, start, end)
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return []


def compute_positions(visible_cuis, focal_cui, radius: float = 360.0):
    """Place focal at (0, 0) and arrange neighbors in a single ring, grouped
    by category in equal-size pie-slice wedges.

    Each present category gets the same angular share, regardless of how many
    nodes it has — this gives every category equal visual weight, so rare
    categories like CIH or Organism Function are not crushed into a sliver.
    Within a wedge, nodes are placed evenly along an arc, sorted by degree
    (most connected → outermost ends of the wedge).
    """
    positions: dict[str, dict[str, float]] = {focal_cui: {"x": 0.0, "y": 0.0}}

    by_cat: dict[str, list[str]] = defaultdict(list)
    for cui in visible_cuis:
        if cui == focal_cui:
            continue
        cat = cui_to_category.get(cui, "Other")
        by_cat[cat].append(cui)

    # Iterate categories in declared order so colors are evenly distributed
    # around the wheel — Drug at top, then Dietary Supplement, CIH, ... clockwise.
    present_cats = [c for c in CATEGORY_COLORS if by_cat.get(c)]
    if not present_cats:
        return positions

    wedge_size = 2 * math.pi / len(present_cats)
    angle = -math.pi / 2  # start at the top of the circle

    for cat in present_cats:
        nodes = sorted(by_cat[cat], key=lambda n: G.degree(n), reverse=True)
        if len(nodes) == 1:
            theta = angle + wedge_size / 2
            positions[nodes[0]] = {
                "x": radius * math.cos(theta),
                "y": radius * math.sin(theta),
            }
        else:
            padding = 0.18  # leave a gap between adjacent wedges
            for i, cui in enumerate(nodes):
                t = padding + (1 - 2 * padding) * (i / (len(nodes) - 1))
                theta = angle + t * wedge_size
                positions[cui] = {
                    "x": radius * math.cos(theta),
                    "y": radius * math.sin(theta),
                }
        angle += wedge_size

    return positions


def build_elements(visible_cuis, focal_cui, hidden_categories, path, focal_only_edges):
    """Construct Cytoscape elements with category-wedge positions, edge
    aggregation, and an optional focal-only edge filter.

    Edges are aggregated by direction: a single (source → target) edge
    carries the full list of predicates between that pair (so 5 parallel
    arrows collapse into 1 edge with `count: 5`). The forward and reverse
    directions are kept as separate edges so direction is still legible.

    When `focal_only_edges` is True, only edges that touch the focal node
    are emitted (path edges are always emitted regardless, so the gold chain
    is never hidden).
    """
    # Path edges as ordered pairs for fast membership tests during edge emission.
    path_pairs = set()
    for i in range(len(path) - 1):
        a, b = path[i], path[i + 1]
        path_pairs.add((a, b))
        path_pairs.add((b, a))
    path_set = set(path)

    # Filter visible nodes by category checklist (focal + path nodes always survive).
    visible_after_filter = []
    for cui in visible_cuis:
        cat = cui_to_category.get(cui, "Other")
        if cat in hidden_categories and cui != focal_cui and cui not in path_set:
            continue
        visible_after_filter.append(cui)

    positions = compute_positions(visible_after_filter, focal_cui)

    nodes = []
    for cui in visible_after_filter:
        cat = cui_to_category.get(cui, "Other")
        full_name = cui_to_name.get(cui, cui)
        nodes.append({
            "data": {
                "id": cui,
                "label": display_label(full_name, cat),
                "full_name": full_name,
                "category": cat,
                "semtype": cui_to_semtype.get(cui, "?"),
                "is_focal": cui == focal_cui,
                "is_path": cui in path_set,
            },
            "position": positions.get(cui, {"x": 0.0, "y": 0.0}),
        })

    # Aggregate edges: one edge per (source, target) direction, carrying the
    # full list of predicates for that pair. Forward and reverse are separate.
    visible_set = {n["data"]["id"] for n in nodes}
    visible_list = list(visible_set)
    edges = []

    def emit(u: str, v: str, predicates: list[str]):
        is_path = (u, v) in path_pairs
        # If focal-only is on, skip cross-links unless they're path edges.
        if focal_only_edges and not is_path and focal_cui not in (u, v):
            return
        count = len(predicates)
        first = predicates[0]
        label = first if count == 1 else f"{first} (+{count - 1})"
        edges.append({
            "data": {
                "source": u,
                "target": v,
                "label": label,
                "predicates": predicates,
                "count": count,
                "is_path": is_path,
            }
        })

    for i, u in enumerate(visible_list):
        for v in visible_list[i + 1:]:
            forward = edge_predicates.get((u, v))
            if forward:
                emit(u, v, forward)
            reverse = edge_predicates.get((v, u))
            if reverse:
                emit(v, u, reverse)

    return nodes + edges


def search_options():
    """Dropdown options — top-degree concepts first, capped to keep the browser snappy."""
    top = sorted(G.nodes, key=lambda n: G.degree(n), reverse=True)[:SEARCH_DROPDOWN_LIMIT]
    return [
        {"label": f"{cui_to_name.get(c, c)}", "value": c}
        for c in top
    ]


SEARCH_OPTIONS = search_options()  # build once

# -----------
# Stylesheet
# -----------

stylesheet = [
    {
        "selector": "node",
        "style": {
            "label": "data(label)",
            "background-color": CATEGORY_COLORS["Other"],
            "color": "#222",
            "font-size": "11px",
            "font-weight": 500,
            "text-wrap": "wrap",
            "text-max-width": "90px",
            "text-valign": "bottom",
            "text-halign": "center",
            "text-margin-y": 4,
            "text-background-color": "#ffffff",
            "text-background-opacity": 0.85,
            "text-background-padding": "2px",
            "text-background-shape": "round-rectangle",
            "width": 36,
            "height": 36,
            "border-width": 2,
            "border-color": "#ffffff",
        },
    },
    {
        "selector": "edge",
        "style": {
            "curve-style": "bezier",
            "line-color": "#bfbfbf",
            "width": 1.2,
            "target-arrow-shape": "triangle",
            "target-arrow-color": "#bfbfbf",
            "arrow-scale": 0.9,
            "opacity": 0.45,
        },
    },
]

# Color rule per category
for category, color in CATEGORY_COLORS.items():
    stylesheet.append({
        "selector": f'node[category = "{category}"]',
        "style": {"background-color": color},
    })

# Focal node — bold border + larger, label centered inside
stylesheet.append({
    "selector": "node[?is_focal]",
    "style": {
        "border-width": 4,
        "border-color": "#222",
        "width": 78,
        "height": 78,
        "font-size": "14px",
        "font-weight": "bold",
        "text-valign": "center",
        "text-margin-y": 0,
    },
})

# Path-highlighted node + edge — gold border, gold edges, predicate label visible
stylesheet.append({
    "selector": "node[?is_path]",
    "style": {
        "border-width": 4,
        "border-color": "#FFD700",
    },
})
stylesheet.append({
    "selector": "edge[?is_path]",
    "style": {
        "line-color": "#FFD700",
        "target-arrow-color": "#FFD700",
        "width": 4,
        "opacity": 1,
        "z-index": 999,
        "label": "data(label)",
        "font-size": "11px",
        "color": "#7a5c00",
        "text-rotation": "autorotate",
        "text-background-color": "#fff",
        "text-background-opacity": 0.85,
        "text-background-padding": "2px",
    },
})

# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

def legend_chip(category: str, color: str):
    return html.Div(
        [
            html.Span(style={
                "display": "inline-block",
                "width": "12px",
                "height": "12px",
                "borderRadius": "50%",
                "backgroundColor": color,
                "marginRight": "5px",
                "verticalAlign": "middle",
            }),
            html.Span(category, style={"fontSize": "12px", "verticalAlign": "middle"}),
        ],
        style={"marginRight": "14px", "display": "inline-block"},
    )


SIDEBAR_STYLE = {
    "width": "320px",
    "padding": "14px",
    "border": "1px solid #e0e0e0",
    "borderRadius": "6px",
    "backgroundColor": "#fafafa",
    "overflowY": "auto",
}
SECTION_LABEL = {"fontWeight": "600", "fontSize": "13px", "marginTop": "10px", "marginBottom": "4px"}
HINT = {"color": "#777", "fontSize": "11px", "marginBottom": "4px"}

initial_visible = stratified_neighbors(ALZHEIMERS_CUI)

app = dash.Dash(__name__)
app.title = "ADInt Explorer"

app.layout = html.Div(
    style={"fontFamily": "system-ui, sans-serif", "padding": "12px"},
    children=[
        # State stores 
        dcc.Store(id="focal-store", data=ALZHEIMERS_CUI),
        dcc.Store(id="visible-store", data=initial_visible),
        dcc.Store(id="path-store", data=[]),

        html.H2("ADInt Explorer", style={"marginBottom": "2px"}),
        html.Div(
            "Interactive visualization of the Zhang lab's ADInt Alzheimer's drug-repurposing knowledge graph",
            style={"color": "#666", "fontSize": "13px", "marginBottom": "8px"},
        ),
        html.Div(
            [legend_chip(c, color) for c, color in CATEGORY_COLORS.items()],
            style={"marginBottom": "10px"},
        ),

        html.Div(
            style={"display": "flex", "gap": "12px", "height": "80vh"},
            children=[
                # Sidebar ( come back to this )
                html.Div(
                    style=SIDEBAR_STYLE,
                    children=[
                        html.Div("Search & refocus", style=SECTION_LABEL),
                        html.Div("Pick a concept to make it the new focal node.", style=HINT),
                        dcc.Dropdown(
                            id="search-dropdown",
                            options=SEARCH_OPTIONS,
                            value=ALZHEIMERS_CUI,
                            clearable=False,
                            style={"fontSize": "12px"},
                        ),

                        html.Div("Show categories", style=SECTION_LABEL),
                        dcc.Checklist(
                            id="category-filter",
                            options=[{"label": " " + c, "value": c} for c in CATEGORY_COLORS],
                            value=list(CATEGORY_COLORS.keys()),
                            style={"fontSize": "12px"},
                            inputStyle={"marginRight": "4px"},
                        ),

                        html.Div("Edge density", style=SECTION_LABEL),
                        dcc.Checklist(
                            id="focal-only-toggle",
                            options=[{"label": " Only edges touching the focal node", "value": "focal_only"}],
                            value=["focal_only"],
                            style={"fontSize": "12px"},
                            inputStyle={"marginRight": "4px"},
                        ),

                        html.Div("Mechanistic path finder", style=SECTION_LABEL),
                        html.Div(
                            "Pick two concepts; the shortest chain through ADInt is highlighted in gold.",
                            style=HINT,
                        ),
                        dcc.Dropdown(
                            id="path-start",
                            options=SEARCH_OPTIONS,
                            placeholder="Start concept",
                            style={"fontSize": "12px", "marginBottom": "4px"},
                        ),
                        dcc.Dropdown(
                            id="path-end",
                            options=SEARCH_OPTIONS,
                            placeholder="End concept",
                            style={"fontSize": "12px"},
                        ),
                        html.Div(
                            [
                                html.Button("Find path", id="path-button", n_clicks=0,
                                            style={"flex": "1", "marginRight": "4px"}),
                                html.Button("Clear", id="path-clear-button", n_clicks=0,
                                            style={"flex": "1"}),
                            ],
                            style={"display": "flex", "marginTop": "6px"},
                        ),
                        html.Div(id="path-status", style={"fontSize": "11px", "color": "#555", "marginTop": "6px"}),

                        html.Div("Edge evidence", style=SECTION_LABEL),
                        html.Div("Click any edge to see the supporting PMID(s) and sentence(s).", style=HINT),
                        html.Div(id="evidence-panel"),

                        html.Div("Tip: click any node to refocus the view on that concept.",
                                 style={"color": "#888", "fontSize": "11px", "marginTop": "16px",
                                        "fontStyle": "italic"}),
                    ],
                ),

                # Main graph
                cyto.Cytoscape(
                    id="adint-graph",
                    elements=[],
                    stylesheet=stylesheet,
                    layout={"name": "preset", "fit": True, "padding": 60},
                    style={
                        "flex": "1",
                        "border": "1px solid #e0e0e0",
                        "borderRadius": "6px",
                        "backgroundColor": "#fafafa",
                    },
                ),
            ],
        ),

        html.Div(id="status-bar", style={"marginTop": "8px", "fontSize": "11px", "color": "#666"}),
    ],
)


# ----------
# Callbacks
# ----------

@app.callback(
    Output("visible-store", "data"),
    Output("focal-store", "data"),
    Input("search-dropdown", "value"),
    Input("adint-graph", "tapNodeData"),
    State("visible-store", "data"),
    State("focal-store", "data"),
    prevent_initial_call=True,
)
def update_visible(search_value, tap_node, current_visible, current_focal):
    """Search OR clicking a node refocuses the view on that concept."""
    triggered = callback_context.triggered_id
    if triggered == "search-dropdown" and search_value:
        new_visible = stratified_neighbors(search_value)
        return new_visible, search_value
    if triggered == "adint-graph" and tap_node:
        cui = tap_node["id"]
        if cui == current_focal:
            return no_update, no_update
        new_visible = stratified_neighbors(cui)
        return new_visible, cui
    return no_update, no_update


@app.callback(
    Output("path-store", "data"),
    Output("visible-store", "data", allow_duplicate=True),
    Output("path-status", "children"),
    Input("path-button", "n_clicks"),
    Input("path-clear-button", "n_clicks"),
    State("path-start", "value"),
    State("path-end", "value"),
    State("visible-store", "data"),
    prevent_initial_call=True,
)
def find_path(_n_find, _n_clear, start, end, current_visible):
    triggered = callback_context.triggered_id
    if triggered == "path-clear-button":
        return [], no_update, ""
    if not start or not end:
        return [], no_update, "Pick both a start and end concept."
    if start == end:
        return [], no_update, "Start and end must differ."
    path = shortest_path(start, end)
    if not path:
        return [], no_update, "No path found in the graph."
    new_visible = list(dict.fromkeys(list(current_visible) + path))
    chain = " → ".join(cui_to_name.get(c, c) for c in path)
    msg = f"Path of length {len(path) - 1}: {chain}"
    return path, new_visible, msg


@app.callback(
    Output("adint-graph", "elements"),
    Output("adint-graph", "layout"),
    Output("status-bar", "children"),
    Input("visible-store", "data"),
    Input("focal-store", "data"),
    Input("category-filter", "value"),
    Input("path-store", "data"),
    Input("focal-only-toggle", "value"),
)
def render_graph(visible, focal, visible_categories, path, focal_only_value):
    hidden = set(CATEGORY_COLORS) - set(visible_categories or [])
    focal_only = "focal_only" in (focal_only_value or [])
    elements = build_elements(visible or [], focal, hidden, path or [], focal_only)
    n_nodes = sum(1 for e in elements if "source" not in e["data"])
    n_edges = len(elements) - n_nodes

    # Per-category neighbor counts so the user can tell when a category is
    # data-limited (e.g. Aspirin only has 5 Gene neighbors total in ADInt).
    cat_counts = defaultdict(int)
    for e in elements:
        if "source" not in e["data"] and not e["data"].get("is_focal"):
            cat_counts[e["data"]["category"]] += 1
    cat_summary = " · ".join(
        f"{c.split()[0] if ' ' in c else c[:4]}: {cat_counts.get(c, 0)}/{PER_CATEGORY_TOP_K}"
        for c in CATEGORY_COLORS if c != "Other"
    )

    status = (
        f"Showing {n_nodes} nodes / {n_edges} edges  ·  "
        f"focal: {cui_to_name.get(focal, focal)}  ·  "
        f"{cat_summary}  ·  "
        f"full ADInt graph: {G.number_of_nodes():,} nodes / {G.number_of_edges():,} edges"
    )
    # Re-run preset layout each render so positions are picked up after refocus.
    layout = {"name": "preset", "fit": True, "padding": 60, "animate": True, "animationDuration": 350}
    return elements, layout, status


def _evidence_rows_for_pair(s_cui: str, o_cui: str, predicates, max_pmids: int = 2):
    """Render evidence rows (predicate header + PMIDs + sentences) for one edge."""
    blocks = []
    for predicate in predicates:
        rows = pd.DataFrame()
        for key in [(s_cui, predicate, o_cui), (o_cui, predicate, s_cui)]:
            if key in evidence_index.groups:
                rows = pd.concat([rows, evidence_index.get_group(key)])
        if rows.empty:
            continue
        rows = rows.head(max_pmids)
        blocks.append(
            html.Div(html.Em(predicate),
                     style={"fontSize": "11px", "color": "#555", "marginTop": "4px"})
        )
        for row in rows.itertuples():
            blocks.append(
                html.Div(
                    [
                        html.A(
                            f"PMID {row.PMID}",
                            href=f"https://pubmed.ncbi.nlm.nih.gov/{row.PMID}/",
                            target="_blank",
                            style={"fontSize": "11px"},
                        ),
                        html.Div(
                            row.SENTENCE,
                            style={"color": "#444", "fontStyle": "italic", "fontSize": "11px",
                                   "marginTop": "2px"},
                        ),
                    ],
                    style={
                        "marginBottom": "6px",
                        "borderLeft": "2px solid #ccc",
                        "paddingLeft": "6px",
                    },
                )
            )
    return blocks


@app.callback(
    Output("evidence-panel", "children"),
    Input("adint-graph", "tapEdgeData"),
    Input("path-store", "data"),
)
def show_evidence(edge_data, path):
    """Two modes, dispatched by which input fired:
      • path-store fired → render evidence for every hop in the highlighted path
      • tapEdgeData fired → render evidence for the single clicked edge
    Without this dispatch, a stale tapEdgeData from an earlier click would
    keep showing while the user runs the path finder.
    """
    triggered = callback_context.triggered_id
    placeholder = html.Div(
        "(click an edge or run the path finder to see supporting literature)",
        style={"color": "#999", "fontSize": "11px"},
    )

    # ---------- path mode ----------
    if triggered == "path-store":
        if not path or len(path) < 2:
            return placeholder
        sections = [
            html.Div(
                "Path evidence",
                style={"fontSize": "12px", "fontWeight": "600", "marginTop": "6px",
                       "marginBottom": "4px"},
            )
        ]
        any_evidence = False
        for i in range(len(path) - 1):
            a, b = path[i], path[i + 1]
            a_name = cui_to_name.get(a, a)
            b_name = cui_to_name.get(b, b)
            forward = edge_predicates.get((a, b), [])
            reverse = edge_predicates.get((b, a), [])
            sections.append(
                html.Div(
                    [html.Strong(a_name), " ↔ ", html.Strong(b_name)],
                    style={"fontSize": "12px", "marginTop": "8px", "marginBottom": "2px",
                           "borderTop": "1px solid #eee", "paddingTop": "6px"},
                )
            )
            hop_blocks = []
            if forward:
                hop_blocks.extend(_evidence_rows_for_pair(a, b, forward, max_pmids=1))
            if reverse:
                hop_blocks.extend(_evidence_rows_for_pair(b, a, reverse, max_pmids=1))
            if hop_blocks:
                sections.extend(hop_blocks)
                any_evidence = True
            else:
                sections.append(
                    html.Div("(no evidence found)",
                             style={"color": "#999", "fontSize": "11px"})
                )
        if not any_evidence:
            return html.Div("(no evidence found for this path)",
                            style={"color": "#999", "fontSize": "11px"})
        return html.Div(sections)

    # edge-click  
    if not edge_data:
        return placeholder
    s_cui = edge_data["source"]
    o_cui = edge_data["target"]
    s_name = cui_to_name.get(s_cui, s_cui)
    o_name = cui_to_name.get(o_cui, o_cui)
    predicates = edge_data.get("predicates")
    if not predicates:
        label = edge_data.get("label", "")
        predicates = [label.split(" (+")[0]] if label else []
    sections = [
        html.Div(
            [html.Strong(s_name), " → ", html.Strong(o_name),
             html.Span(f"  ({len(predicates)} predicate(s))",
                       style={"color": "#888", "fontSize": "10px"})],
            style={"fontSize": "12px", "marginTop": "6px", "marginBottom": "6px"},
        )
    ]
    sections.extend(_evidence_rows_for_pair(s_cui, o_cui, predicates, max_pmids=2))
    if len(sections) == 1:
        return html.Div("(no evidence found)", style={"color": "#999", "fontSize": "11px"})
    return html.Div(sections)


if __name__ == "__main__":
    app.run(debug=True)

"""Generate executive-style strategy PDF report."""
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_JUSTIFY
from reportlab.lib.colors import HexColor, black, white, lightgrey
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, PageBreak, Table, TableStyle,
    KeepTogether, ListFlowable, ListItem, Frame, PageTemplate, BaseDocTemplate,
    NextPageTemplate
)
from reportlab.platypus.flowables import HRFlowable
from datetime import datetime

# ===========================================================================
# COLOR SYSTEM (Goldman/Blackstone aesthetic — navy, charcoal, gold accents)
# ===========================================================================
NAVY = HexColor("#0C2340")        # primary brand
DEEP_NAVY = HexColor("#081A30")   # darker for backgrounds
CHARCOAL = HexColor("#3A3A3A")    # body text
GOLD = HexColor("#B89D5E")        # accents
LIGHT_GOLD = HexColor("#F5EFDD")  # callout backgrounds
SOFT_GREY = HexColor("#F5F5F5")   # alternating rows
MEDIUM_GREY = HexColor("#888888")
SUCCESS_GREEN = HexColor("#1B5E20")
WARNING_RED = HexColor("#B71C1C")
TABLE_BORDER = HexColor("#D5D5D5")

# ===========================================================================
# STYLES
# ===========================================================================
styles = getSampleStyleSheet()

# Title
title_style = ParagraphStyle(
    "TitleStyle", parent=styles["Title"],
    fontName="Helvetica-Bold", fontSize=28, leading=34,
    textColor=white, alignment=TA_CENTER, spaceAfter=12,
)
subtitle_style = ParagraphStyle(
    "SubtitleStyle", parent=styles["Normal"],
    fontName="Helvetica", fontSize=14, leading=18,
    textColor=GOLD, alignment=TA_CENTER, spaceAfter=8,
)
date_style = ParagraphStyle(
    "DateStyle", parent=styles["Normal"],
    fontName="Helvetica", fontSize=10, leading=14,
    textColor=HexColor("#CCCCCC"), alignment=TA_CENTER,
)

# Section headers
h1_style = ParagraphStyle(
    "H1", parent=styles["Heading1"],
    fontName="Helvetica-Bold", fontSize=18, leading=22,
    textColor=NAVY, spaceBefore=18, spaceAfter=10,
    borderPadding=(0, 0, 6, 0),
)
h2_style = ParagraphStyle(
    "H2", parent=styles["Heading2"],
    fontName="Helvetica-Bold", fontSize=13, leading=17,
    textColor=NAVY, spaceBefore=14, spaceAfter=6,
)
h3_style = ParagraphStyle(
    "H3", parent=styles["Heading3"],
    fontName="Helvetica-Bold", fontSize=11, leading=14,
    textColor=GOLD, spaceBefore=10, spaceAfter=4,
)

# Body
body_style = ParagraphStyle(
    "Body", parent=styles["Normal"],
    fontName="Helvetica", fontSize=10, leading=14,
    textColor=CHARCOAL, alignment=TA_JUSTIFY, spaceAfter=6,
)
body_no_just = ParagraphStyle(
    "BodyNJ", parent=body_style, alignment=TA_LEFT,
)

# Callout / quote
callout_style = ParagraphStyle(
    "Callout", parent=body_style,
    backColor=LIGHT_GOLD, borderColor=GOLD, borderWidth=0,
    leftIndent=10, rightIndent=10, spaceBefore=8, spaceAfter=10,
    fontName="Helvetica-Oblique",
    borderPadding=(8, 10, 8, 10),
)

# Code block (math, formulas, key constants)
code_style = ParagraphStyle(
    "Code", parent=styles["Code"],
    fontName="Courier", fontSize=9, leading=12,
    backColor=HexColor("#F0F2F5"), borderColor=HexColor("#DDE1E7"),
    borderWidth=0.5, leftIndent=10, rightIndent=10,
    borderPadding=(6, 8, 6, 8), spaceBefore=4, spaceAfter=10,
    textColor=DEEP_NAVY,
)

# Bullet style
bullet_style = ParagraphStyle(
    "Bullet", parent=body_style,
    leftIndent=18, bulletIndent=6, spaceAfter=4,
)

# Caption (small text under tables/figures)
caption_style = ParagraphStyle(
    "Caption", parent=body_style,
    fontSize=8.5, leading=11, textColor=MEDIUM_GREY,
    alignment=TA_CENTER, spaceBefore=2, spaceAfter=12, fontName="Helvetica-Oblique",
)

# Verdict callout (box with strong message)
verdict_style = ParagraphStyle(
    "Verdict", parent=body_style,
    fontName="Helvetica-Bold", fontSize=10.5, leading=14,
    textColor=NAVY, alignment=TA_LEFT,
    borderPadding=(10, 12, 10, 12),
    backColor=HexColor("#EDF1F8"), borderColor=NAVY, borderWidth=0,
    spaceBefore=8, spaceAfter=12,
)


# ===========================================================================
# PAGE TEMPLATES
# ===========================================================================
def cover_page(canvas, doc):
    """Dark navy cover with gold accent."""
    canvas.saveState()
    # Background
    canvas.setFillColor(DEEP_NAVY)
    canvas.rect(0, 0, letter[0], letter[1], stroke=0, fill=1)
    # Gold horizontal rule
    canvas.setStrokeColor(GOLD)
    canvas.setLineWidth(2)
    canvas.line(1.5*inch, 6*inch, letter[0] - 1.5*inch, 6*inch)
    canvas.line(1.5*inch, 3.5*inch, letter[0] - 1.5*inch, 3.5*inch)
    # Footer text
    canvas.setFillColor(GOLD)
    canvas.setFont("Helvetica", 9)
    canvas.drawCentredString(letter[0]/2, 0.6*inch,
                              "CONFIDENTIAL — INTERNAL STRATEGY REVIEW")
    canvas.restoreState()


def content_page(canvas, doc):
    """Content pages — simple header rule + footer with page number."""
    canvas.saveState()
    # Header
    canvas.setStrokeColor(NAVY)
    canvas.setLineWidth(0.75)
    canvas.line(0.75*inch, letter[1] - 0.6*inch,
                letter[0] - 0.75*inch, letter[1] - 0.6*inch)
    canvas.setFillColor(NAVY)
    canvas.setFont("Helvetica-Bold", 9)
    canvas.drawString(0.75*inch, letter[1] - 0.5*inch,
                      "POLYMARKET WEATHER STRATEGY")
    canvas.setFillColor(GOLD)
    canvas.setFont("Helvetica", 9)
    canvas.drawRightString(letter[0] - 0.75*inch, letter[1] - 0.5*inch,
                            "Comprehensive Strategy Report")
    # Footer
    canvas.setStrokeColor(HexColor("#DDDDDD"))
    canvas.setLineWidth(0.5)
    canvas.line(0.75*inch, 0.6*inch, letter[0] - 0.75*inch, 0.6*inch)
    canvas.setFillColor(MEDIUM_GREY)
    canvas.setFont("Helvetica", 8)
    canvas.drawString(0.75*inch, 0.4*inch,
                      "Apr 25 2026 — Post-flip refactor")
    canvas.drawRightString(letter[0] - 0.75*inch, 0.4*inch,
                            f"Page {doc.page}")
    canvas.restoreState()


# ===========================================================================
# DOCUMENT BUILD
# ===========================================================================
output_path = "/sessions/gracious-beautiful-brahmagupta/mnt/polymarket_strat/strategy_report.pdf"

doc = BaseDocTemplate(
    output_path, pagesize=letter,
    leftMargin=0.85*inch, rightMargin=0.85*inch,
    topMargin=0.85*inch, bottomMargin=0.85*inch,
    title="Polymarket Weather Strategy — Comprehensive Strategy Report",
    author="Wonsang Jang × Quant System",
)

cover_frame = Frame(
    0.75*inch, 0.75*inch,
    letter[0] - 1.5*inch, letter[1] - 1.5*inch,
    leftPadding=0, rightPadding=0,
    topPadding=0, bottomPadding=0,
)
content_frame = Frame(
    0.75*inch, 0.75*inch,
    letter[0] - 1.5*inch, letter[1] - 1.65*inch,
    leftPadding=0, rightPadding=0,
    topPadding=0, bottomPadding=0,
)

doc.addPageTemplates([
    PageTemplate(id="Cover", frames=[cover_frame], onPage=cover_page),
    PageTemplate(id="Content", frames=[content_frame], onPage=content_page),
])

story = []


# ===========================================================================
# COVER PAGE
# ===========================================================================
story.append(Spacer(1, 1.8*inch))
story.append(Paragraph("POLYMARKET", subtitle_style))
story.append(Paragraph("WEATHER ALPHA STRATEGY", title_style))
story.append(Spacer(1, 0.3*inch))
story.append(Paragraph("Comprehensive Strategy Report", subtitle_style))
story.append(Spacer(1, 1.5*inch))
story.append(Paragraph(
    "Pricing logic, position sizing, decision waterfalls,<br/>"
    "and the post-paradigm-flip refactor",
    ParagraphStyle("CoverSub", parent=subtitle_style, fontSize=11,
                   textColor=HexColor("#DDDDDD"), leading=18),
))
story.append(Spacer(1, 0.4*inch))
story.append(Paragraph(
    f"Prepared for: 장원상 (Wonsang Jang)<br/>"
    f"Date: {datetime.now().strftime('%B %d, %Y')}<br/>"
    f"Mode: Phase 1 Paper / EC2 Production",
    date_style,
))

story.append(NextPageTemplate("Content"))
story.append(PageBreak())


# ===========================================================================
# EXECUTIVE SUMMARY
# ===========================================================================
story.append(Paragraph("Executive Summary", h1_style))
story.append(HRFlowable(width="100%", thickness=2, color=GOLD,
                         spaceBefore=0, spaceAfter=10))

story.append(Paragraph(
    "This report documents every pricing and positioning decision in the "
    "Polymarket weather alpha strategy as of April 25 2026. The system "
    "is a fully automated quantitative trading pipeline targeting "
    "binary temperature-bracket contracts on Polymarket, currently in "
    "Phase 1 paper-trading mode with $200 simulated capital.",
    body_style,
))

story.append(Paragraph(
    "Over the past 72 hours the system underwent three major "
    "philosophical evolutions: from naive model-vs-market edge-finding, "
    "through a defensive throttle response after −$687 cumulative "
    "drawdown, to a final efficient-markets paradigm that treats the "
    "market price as the prior and our forecast model as one piece of "
    "evidence among many.",
    body_style,
))

story.append(Paragraph(
    "The current production pipeline applies <b>26 sequential decision "
    "gates</b> across discovery, analysis, execution, and rebalancing. "
    "It supports three distinct edge sources: pure-arithmetic arbitrage, "
    "ensemble-confidence-weighted model edges, and a Claude API "
    "qualitative review layer.",
    body_style,
))

story.append(Spacer(1, 0.1*inch))
story.append(Paragraph("KEY METRICS — APRIL 25 2026", h3_style))

# Metrics table
metrics_data = [
    ["Metric", "Value", "Notes"],
    ["Cities tradeable", "11 of 22", "11 blocklisted via Plan B"],
    ["Cumulative paper P&L", "−$687.29", "−9% of starting bankroll"],
    ["Win rate (settled)", "9.1%", "3W / 30L over 33 settled"],
    ["Tests passing", "382 / 382", "0 failures"],
    ["Decision gates active", "26", "Across all 5 pipeline phases"],
    ["Distinct edge strategies", "3", "S1 arb, S3 blend, S7 Claude"],
    ["EC2 deployment status", "Live", "Tailscale + cron"],
]

metrics_table = Table(metrics_data, colWidths=[2.0*inch, 1.6*inch, 2.7*inch])
metrics_table.setStyle(TableStyle([
    ("BACKGROUND", (0, 0), (-1, 0), NAVY),
    ("TEXTCOLOR", (0, 0), (-1, 0), white),
    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
    ("FONTSIZE", (0, 0), (-1, 0), 10),
    ("ALIGN", (0, 0), (-1, 0), "LEFT"),
    ("ALIGN", (1, 1), (1, -1), "LEFT"),
    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ("FONTSIZE", (0, 1), (-1, -1), 9.5),
    ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
    ("FONTNAME", (1, 1), (1, -1), "Helvetica-Bold"),
    ("TEXTCOLOR", (1, 1), (1, -1), NAVY),
    ("LEFTPADDING", (0, 0), (-1, -1), 8),
    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ("TOPPADDING", (0, 0), (-1, -1), 8),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [white, SOFT_GREY]),
    ("GRID", (0, 0), (-1, -1), 0.4, TABLE_BORDER),
]))
story.append(metrics_table)

story.append(Spacer(1, 0.15*inch))

story.append(Paragraph("THE CORE INSIGHT", h3_style))
story.append(Paragraph(
    '"The market price is the Bayesian prior — the weighted average '
    'of all participants\' beliefs and information. Our model is one '
    'piece of evidence among many. Edge exists in identifiable '
    'inefficiencies, not in average disagreement."',
    callout_style,
))

story.append(PageBreak())


# ===========================================================================
# ARCHITECTURE OVERVIEW
# ===========================================================================
story.append(Paragraph("System Architecture", h1_style))
story.append(HRFlowable(width="100%", thickness=2, color=GOLD,
                         spaceBefore=0, spaceAfter=10))

story.append(Paragraph(
    "The system runs as an hourly cron job on AWS EC2 (Seoul region), "
    "with each tick passing through five sequential phases. Every phase "
    "has its own decision logic; the analyze phase contains the bulk of "
    "the intelligence.",
    body_style,
))

story.append(Spacer(1, 0.1*inch))

# Phase pipeline table
phases_data = [
    ["Phase", "Purpose", "Output"],
    ["A. Discover",
     "Scan Polymarket events; parse weather brackets",
     "300-600 BracketContract objects"],
    ["B. Rebalance",
     "Review existing open positions; exit losers/winners",
     "0-N exits with reasons"],
    ["C. Snapshot",
     "Record current market prices for posthoc analysis",
     "market_prices DB rows"],
    ["D. Analyze",
     "Apply 22 decision gates per contract",
     "Trade plans for surviving signals"],
    ["E. Execute",
     "Place trades; Claude gate; record diagnostics",
     "Filled orders + rich metadata"],
]
phases_table = Table(phases_data, colWidths=[1.3*inch, 3.0*inch, 2.0*inch])
phases_table.setStyle(TableStyle([
    ("BACKGROUND", (0, 0), (-1, 0), NAVY),
    ("TEXTCOLOR", (0, 0), (-1, 0), white),
    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
    ("FONTNAME", (0, 1), (0, -1), "Helvetica-Bold"),
    ("TEXTCOLOR", (0, 1), (0, -1), NAVY),
    ("FONTSIZE", (0, 0), (-1, -1), 9.5),
    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ("LEFTPADDING", (0, 0), (-1, -1), 8),
    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ("TOPPADDING", (0, 0), (-1, -1), 8),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [white, SOFT_GREY]),
    ("GRID", (0, 0), (-1, -1), 0.4, TABLE_BORDER),
]))
story.append(phases_table)

story.append(Spacer(1, 0.2*inch))

story.append(Paragraph("KEY DATA STRUCTURES", h3_style))
story.append(Paragraph(
    "<b>BracketContract</b> — single binary contract: city, target_date, "
    "lower/upper temperature bounds, market prices (best_ask, best_bid, "
    "spread, liquidity), token IDs for YES and NO sides.",
    body_style,
))
story.append(Paragraph(
    "<b>ErrorDistribution</b> — fitted parametric distribution of "
    "(forecast − observed) errors, sliced by (city, model, regime, "
    "lead_hours, season). Family is Normal / Skew-Normal / Student-t. "
    "Fit via MLE on historical observations.",
    body_style,
))
story.append(Paragraph(
    "<b>TradePlan</b> — decision artifact: target_notional, side "
    "(BUY_YES or BUY_NO), token_id, expected_value, full diagnostics "
    "metadata for posthoc analysis.",
    body_style,
))

story.append(PageBreak())


# ===========================================================================
# PHASE A — DISCOVERY
# ===========================================================================
story.append(Paragraph("Phase A — Contract Discovery", h1_style))
story.append(HRFlowable(width="100%", thickness=2, color=GOLD,
                         spaceBefore=0, spaceAfter=10))

story.append(Paragraph(
    "Polymarket exposes thousands of active markets via its Gamma API. "
    "The discovery phase filters these down to weather-bracket contracts "
    "and parses their structure into our internal BracketContract type.",
    body_style,
))

story.append(Paragraph("PROCESS", h3_style))
story.append(Paragraph(
    "1. Paginate <code>/events?active=true&closed=false</code> up to 5,000 "
    "events.<br/>"
    "2. Filter event titles for weather keywords (temperature, °F, °C).<br/>"
    "3. Walk each event's child markets (bracket contracts).<br/>"
    "4. Parse the question text to extract <i>city</i>, <i>target_date</i>, "
    "<i>lower bound</i>, <i>upper bound</i>.<br/>"
    "5. Apply defensive filters: drop expired contracts (endDateIso "
    "in past), drop closed/non-accepting contracts, deduplicate by "
    "<code>conditionId</code>.",
    body_style,
))

story.append(Paragraph("EXAMPLE PARSE", h3_style))
story.append(Paragraph(
    "<b>Question:</b> \"Will the highest temperature in Tokyo on April "
    "25 be 18°C or higher?\"<br/>"
    "<b>Output:</b> <code>BracketContract(city='tokyo', "
    "target_date=2026-04-25, lower_f=64.4, upper_f=200.0)</code>",
    code_style,
))

story.append(Paragraph(
    "Typical output: 300-600 contracts per cycle across 22 cities. After "
    "the Plan B blocklist filters 11 cities, ~100-200 contracts proceed "
    "to Phase D analysis.",
    body_style,
))

story.append(PageBreak())


# ===========================================================================
# PHASE D — ANALYZE: THE BRAIN
# ===========================================================================
story.append(Paragraph("Phase D — Analyze (the Brain)", h1_style))
story.append(HRFlowable(width="100%", thickness=2, color=GOLD,
                         spaceBefore=0, spaceAfter=10))

story.append(Paragraph(
    "This is where 90% of the intelligence lives. Each contract passes "
    "through 22 sequential gates. Earlier gates are cheap to evaluate; "
    "expensive operations (forecast fetches, distribution lookups) are "
    "deferred until cheap rejections have done their work.",
    body_style,
))

story.append(Paragraph(
    "Below is the full waterfall, in execution order.",
    body_style,
))

story.append(Spacer(1, 0.05*inch))

# The big gate table
gates_data = [
    ["#", "Gate / Step", "What It Does", "Reject Reason"],
    ["D1", "Coherence Arbitrage", "Detect monotonicity violations across bracket families", "Pure arb fires"],
    ["D2", "City + Date Group", "Group brackets sharing weather event", "—"],
    ["D3", "Hard Blocklist", "Skip 11 manually-blocked cities", "blocked_city"],
    ["D4", "Time-to-Settlement", "Reject < 3h, late-entry mode 3-6h, normal 6-48h", "too_close / beyond_horizon"],
    ["D5", "Bucket-EV Auto-Block", "Skip (city, lead, regime) buckets with realized EV<0", "bucket_ev_blocked"],
    ["D6", "Fetch Forecasts", "Pull GFS/ECMWF/HRRR/NAM from Open-Meteo", "no_forecast"],
    ["D7", "Regime Classification", "Classify weather: stable / frontal / transition / etc.", "—"],
    ["D8", "Load Error Distribution", "Lookup (city,model,regime,lead,season) distribution", "insufficient_samples"],
    ["D9", "Apply σ Floor", "Three-tier (1.5/2.0/2.5°F) based on n_samples", "—"],
    ["D10", "Compute Bracket P", "CDF math + ensemble weighting", "—"],
    ["D11", "Apply Isotonic", "Post-hoc calibration remap from walk-forward", "—"],
    ["D12", "Per-Side Edge", "Evaluate BOTH YES and NO sides", "—"],
    ["D13", "Market Band Gate", "p_market ∈ [0.15, 0.75]", "gate1_market_band"],
    ["D14", "P-Scaled Min Edge", "5¢ at p≥0.5, 10¢ at p<0.5", "gate2_min_edge*"],
    ["D15", "Min Entry Price", "Reject < 0.02 (filter 50× leverage)", "min_entry_price"],
    ["D16", "Plan B High-P Cap", "Reject p>0.85 AND edge>0.20", "plan_b_high_p_artifact"],
    ["D17", "Strategy 3: Blend", "Ensemble-confidence-weighted with market", "s3_low_confidence / s3_blended_edge_too_small"],
    ["D18", "Late-Entry Conviction", "If 3-6h lead: require p extreme + edge ≥ 20¢", "late_entry_*"],
    ["D19", "Compute Kelly", "Quarter-Kelly with CV² shrinkage", "—"],
    ["D20", "Reliability Sizing", "min(1, 0.12/brier) × min(1, n/50)", "—"],
    ["D21", "Final Notional", "Apply caps: bankroll × 2.5%, $5 minimum", "—"],
    ["D22", "Correlation Cap", "Group exposure < 15% of bankroll", "group_cap"],
    ["D23", "Liquidity Depth", "target_notional ≤ 0.15 × liquidity", "depth_*"],
    ["D24", "Min Order Size", "Final size ≥ $5", "min_order_size"],
]

gates_table = Table(gates_data, colWidths=[0.45*inch, 1.5*inch, 2.4*inch, 1.95*inch])
gates_table.setStyle(TableStyle([
    ("BACKGROUND", (0, 0), (-1, 0), NAVY),
    ("TEXTCOLOR", (0, 0), (-1, 0), white),
    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
    ("FONTSIZE", (0, 0), (-1, 0), 9),
    ("FONTSIZE", (0, 1), (-1, -1), 7.5),
    ("FONTNAME", (1, 1), (1, -1), "Helvetica-Bold"),
    ("TEXTCOLOR", (1, 1), (1, -1), NAVY),
    ("FONTNAME", (3, 1), (3, -1), "Courier"),
    ("FONTSIZE", (3, 1), (3, -1), 6.8),
    ("TEXTCOLOR", (3, 1), (3, -1), HexColor("#7B1818")),
    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ("LEFTPADDING", (0, 0), (-1, -1), 5),
    ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ("TOPPADDING", (0, 0), (-1, -1), 4),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [white, SOFT_GREY]),
    ("GRID", (0, 0), (-1, -1), 0.3, TABLE_BORDER),
]))
story.append(gates_table)
story.append(Paragraph(
    "Decision gates D1-D24 in execution order. Earlier rejections "
    "are cheaper. Asterisk reasons (gate2_min_edge*) split into low-p "
    "and high-p variants for diagnostic granularity.",
    caption_style,
))

story.append(PageBreak())


# ===========================================================================
# CORE MATH — bracket probability
# ===========================================================================
story.append(Paragraph("Core Mathematics — Bracket Probability", h1_style))
story.append(HRFlowable(width="100%", thickness=2, color=GOLD,
                         spaceBefore=0, spaceAfter=10))

story.append(Paragraph("BRACKET CDF", h3_style))
story.append(Paragraph(
    "For a bracket [L, U] in degrees Fahrenheit, weather forecast F, "
    "and fitted error distribution D ~ (μ, σ, family), where "
    "<code>error = F - observed</code>:",
    body_style,
))
story.append(Paragraph(
    "P(observed ∈ [L, U]) = CDF_D(F - L) - CDF_D(F - U)",
    code_style,
))
story.append(Paragraph(
    "Each model (GFS, ECMWF, HRRR, NAM) gives its own probability. "
    "These are blended via skill-weighted ensemble:",
    body_style,
))
story.append(Paragraph(
    "p_model = Σ_i w_i × P_i &nbsp;&nbsp;where w_GFS=0.30, w_ECMWF=0.40, "
    "w_HRRR=0.20, w_NAM=0.10",
    code_style,
))

story.append(Paragraph("ISOTONIC CALIBRATION", h3_style))
story.append(Paragraph(
    "The raw ensemble probability is then remapped through a learned "
    "monotonic curve. We fit this from walk-forward backtest data — "
    "if historical evidence shows \"when model says 30%, reality is "
    "10%\", the isotonic transform encodes this and outputs the "
    "honest probability.",
    body_style,
))

story.append(Paragraph("EDGE AFTER FEES", h3_style))
story.append(Paragraph(
    "Polymarket charges 2% on WINNINGS only:",
    body_style,
))
story.append(Paragraph(
    "edge_after_fees = (p_model - p_market) - 0.02 × p_model × (1 - p_market)",
    code_style,
))

story.append(Paragraph("STRATEGY 3 — ENSEMBLE-CONFIDENCE BLEND", h3_style))
story.append(Paragraph(
    "When ensemble members disagree (high σ), our model is unreliable. "
    "We adaptively blend with the market price using:",
    body_style,
))
story.append(Paragraph(
    "model_confidence = exp(-σ_ensemble / 3.0)<br/>"
    "w = model_confidence²<br/>"
    "p_blended = w × p_model + (1 - w) × p_market<br/>"
    "blended_edge = p_blended - p_market - fee_drag",
    code_style,
))
story.append(Paragraph(
    "This automatically defers to market consensus on storm days "
    "where model trust is low. Trade gate: <b>confidence ≥ 0.50</b> "
    "AND <b>blended_edge ≥ 0.08 (8¢)</b>.",
    body_style,
))

story.append(Paragraph("KELLY POSITION SIZING", h3_style))
story.append(Paragraph(
    "Quarter-Kelly with uncertainty shrinkage:",
    body_style,
))
story.append(Paragraph(
    "f* = (p × b - q) / b &nbsp; where b = (1 - P_market)(1 - fee) / P_market<br/>"
    "f_kelly = 0.25 × f* × 1/(1 + CV²)<br/>"
    "f_final = f_kelly × reliability_multiplier × late_entry_mult",
    code_style,
))

story.append(Paragraph("RELIABILITY MULTIPLIER", h3_style))
story.append(Paragraph(
    "Per-city sizing shrinkage:",
    body_style,
))
story.append(Paragraph(
    "reliability = min(1, 0.12 / city_brier) × min(1, n_samples / 50)",
    code_style,
))

story.append(Paragraph("BREAKEVEN EXIT (REBALANCE)", h3_style))
story.append(Paragraph(
    "Exit when current model probability falls below the EV-neutral "
    "threshold:",
    body_style,
))
story.append(Paragraph(
    "P*_breakeven = (0.98 × best_bid + 0.02 × entry_price) /<br/>"
    "&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;(0.98 + 0.02 × entry_price)",
    code_style,
))

story.append(PageBreak())


# ===========================================================================
# THE THREE STRATEGIES
# ===========================================================================
story.append(Paragraph("The Three Edge Strategies", h1_style))
story.append(HRFlowable(width="100%", thickness=2, color=GOLD,
                         spaceBefore=0, spaceAfter=10))

story.append(Paragraph(
    "Following the −$687 drawdown audit, we acknowledged that the "
    "implicit assumption \"our model is right, market is wrong\" was "
    "the root failure. The post-flip refactor introduces three distinct "
    "edge sources that respect efficient markets:",
    body_style,
))

story.append(Spacer(1, 0.1*inch))

# Strategy 1
story.append(Paragraph("STRATEGY 1 — CROSS-BRACKET COHERENCE ARBITRAGE", h2_style))
story.append(Paragraph(
    "Pure arithmetic edge — no forecast model required. For \"X or "
    "higher\" brackets at thresholds T₁ &lt; T₂ &lt; T₃, market prices "
    "MUST satisfy: P(≥T₁) ≥ P(≥T₂) ≥ P(≥T₃). When markets violate "
    "this, buy the cheaper bracket — guaranteed mathematical edge.",
    body_style,
))
story.append(Paragraph(
    "<b>When it fires:</b> rare (estimated 10-30 events/week) but "
    "~100% win rate when found. Sized at $5 per arb. No model "
    "validation, no Claude gate.",
    body_style,
))

# Strategy 3
story.append(Paragraph("STRATEGY 3 — ENSEMBLE-CONFIDENCE-WEIGHTED BLEND", h2_style))
story.append(Paragraph(
    "When forecast models AGREE, our model is well-supported and we "
    "trade at our model's probability. When they DISAGREE, we adaptively "
    "blend toward market consensus via the formula above.",
    body_style,
))
story.append(Paragraph(
    "<b>Killer feature:</b> automatically defeats the \"p=1.00 artifact\" "
    "pattern that bled us $687. A bracket-parser bug that produces "
    "p=1.00 with edge=30¢ on a stormy day (high ensemble σ) gets "
    "blended down to p_market, blended_edge ≈ 0¢, rejected.",
    body_style,
))

# Strategy 7
story.append(Paragraph("STRATEGY 7 — CLAUDE API QUALITATIVE GATE", h2_style))
story.append(Paragraph(
    "After all quantitative gates approve a trade, send the full context "
    "to Claude (Sonnet 4.6). Ask: \"Is this real edge or an artifact?\" "
    "Get structured JSON: <code>{is_real_edge, confidence, reasoning, "
    "red_flags}</code>. Trade fires only if approved with confidence ≥ 0.6.",
    body_style,
))
story.append(Paragraph(
    "<b>What it catches:</b> semantic anomalies our quant pipeline misses — "
    "climatology-impossible signals (Dubai ≤5°C in July), bracket-question "
    "vs bounds mismatches, stale-forecast trade structures.",
    body_style,
))
story.append(Paragraph(
    "<b>Failure modes:</b> API down → fail-OPEN (trade passes, since "
    "quant pipeline already approved). Cost ≈ $0.05/call × 5-10 "
    "trades/day = $7-15/month. Trivial.",
    body_style,
))

story.append(Spacer(1, 0.1*inch))

story.append(Paragraph("STRATEGY COMPOSITION", h3_style))

strategy_comp_data = [
    ["Layer", "Strategy", "Decision"],
    ["Pre-screen", "S1 Coherence Arbitrage", "Auto-fire if found (model-free)"],
    ["Regime classification", "Implicit (regime-aware fits)", "Use season + regime distributions"],
    ["Confidence weighting", "S3 Ensemble blend", "Blend p_model with p_market based on σ"],
    ["Final qualitative gate", "S7 Claude API", "Veto remaining false positives"],
]
strategy_comp_table = Table(strategy_comp_data, colWidths=[1.5*inch, 1.85*inch, 2.95*inch])
strategy_comp_table.setStyle(TableStyle([
    ("BACKGROUND", (0, 0), (-1, 0), NAVY),
    ("TEXTCOLOR", (0, 0), (-1, 0), white),
    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
    ("FONTSIZE", (0, 0), (-1, -1), 9),
    ("FONTNAME", (0, 1), (0, -1), "Helvetica-Bold"),
    ("TEXTCOLOR", (0, 1), (0, -1), GOLD),
    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ("LEFTPADDING", (0, 0), (-1, -1), 8),
    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ("TOPPADDING", (0, 0), (-1, -1), 6),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [white, SOFT_GREY]),
    ("GRID", (0, 0), (-1, -1), 0.4, TABLE_BORDER),
]))
story.append(strategy_comp_table)

story.append(PageBreak())


# ===========================================================================
# REBALANCE PHASE
# ===========================================================================
story.append(Paragraph("Phase B — Rebalance Logic", h1_style))
story.append(HRFlowable(width="100%", thickness=2, color=GOLD,
                         spaceBefore=0, spaceAfter=10))

story.append(Paragraph(
    "Every cron tick, before scanning for new trades, we review every "
    "open position. The rebalance decides whether to hold or exit "
    "based on a four-trigger waterfall:",
    body_style,
))

story.append(Spacer(1, 0.05*inch))

rebalance_data = [
    ["Priority", "Trigger", "Condition", "Reason"],
    ["1", "Breakeven Triggered", "current_model_prob < P*_breakeven", "Model says hold, market disagrees"],
    ["2", "Profit Take", "best_bid - entry_price ≥ 0.20", "Book the win, avoid mean-reversion"],
    ["3", "Edge Drop (Fresh)", "edge_drop ≥ 0.10 AND hash changed", "Fresh forecast info says we're wrong"],
    ["4", "Edge Drop (Stale)", "edge_drop ≥ 0.15 AND hash unchanged", "Market-only move past tolerance"],
    ["—", "Hold", "Otherwise", "Continue to settlement"],
]

rebalance_table = Table(rebalance_data, colWidths=[0.7*inch, 1.5*inch, 2.3*inch, 1.85*inch])
rebalance_table.setStyle(TableStyle([
    ("BACKGROUND", (0, 0), (-1, 0), NAVY),
    ("TEXTCOLOR", (0, 0), (-1, 0), white),
    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
    ("FONTSIZE", (0, 0), (-1, 0), 9.5),
    ("FONTSIZE", (0, 1), (-1, -1), 8.5),
    ("FONTNAME", (0, 1), (0, -1), "Helvetica-Bold"),
    ("FONTNAME", (1, 1), (1, -1), "Helvetica-Bold"),
    ("TEXTCOLOR", (1, 1), (1, -1), NAVY),
    ("FONTNAME", (2, 1), (2, -1), "Courier"),
    ("FONTSIZE", (2, 1), (2, -1), 7.5),
    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ("ALIGN", (0, 0), (0, -1), "CENTER"),
    ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ("TOPPADDING", (0, 0), (-1, -1), 6),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [white, SOFT_GREY]),
    ("GRID", (0, 0), (-1, -1), 0.4, TABLE_BORDER),
]))
story.append(rebalance_table)

story.append(Spacer(1, 0.1*inch))

story.append(Paragraph(
    "<b>Why this order matters:</b> Breakeven (rigorous, model-aware) "
    "fires before profit-take (heuristic 20¢ rule), which fires before "
    "edge-drop. Each is logically a sufficient condition; ordering by "
    "rigor preserves the most-defensible decision.",
    body_style,
))

story.append(Paragraph(
    "<b>Exit P&L (paper mode):</b> shares × (best_bid − entry_price) "
    "with 2% fee on positive gains only. For NO positions the formula "
    "is symmetric — entry_price and best_bid are NO-token native, "
    "win/loss flips at settlement.",
    body_style,
))

story.append(Paragraph("EXIT MARK CONVENTIONS", h3_style))
story.append(Paragraph(
    "<b>current_edge</b> uses best_ask (conservative): \"could I "
    "re-enter now?\". <b>realized P&L</b> uses best_bid (fair): "
    "\"what would I actually receive?\". Asymmetric on purpose.",
    body_style,
))

story.append(Paragraph(
    "After exit, the token is locked for 3 hours (cooldown), aligned "
    "with GFS run cadence (00Z, 06Z, 12Z, 18Z) so we don't whipsaw "
    "on the same ticker.",
    body_style,
))

story.append(PageBreak())


# ===========================================================================
# DIALS / KNOBS
# ===========================================================================
story.append(Paragraph("System Dials & Knobs", h1_style))
story.append(HRFlowable(width="100%", thickness=2, color=GOLD,
                         spaceBefore=0, spaceAfter=10))

story.append(Paragraph(
    "These are every tunable parameter that controls the strategy. "
    "Adjusting these is the primary lever for shifting risk profile "
    "and signal volume.",
    body_style,
))

dials_data = [
    ["Parameter", "Value", "Effect"],
    ["BLOCKED_CITIES", "11 cities", "Hard skip"],
    ["N_MIN_FOR_TRADING", "30", "Min samples for fit to be tradeable"],
    ["Market band lower / upper", "0.15 / 0.75", "Penny floor / crushed-payoff cap"],
    ["Min edge (high-p / low-p)", "5¢ / 10¢", "After fees, p≥0.5 vs p<0.5"],
    ["PLAN_B_HIGH_P_CAP", "0.85", "Reject if p>0.85 AND edge>0.20"],
    ["S3_SIGMA_DECAY", "3.0°F", "Confidence decay rate"],
    ["S3_MIN_CONFIDENCE", "0.50", "Required to trade"],
    ["S3_MIN_BLENDED_EDGE", "8¢", "After ensemble blend"],
    ["LATE_ENTRY_MIN_LEAD_H", "3.0", "Hard floor"],
    ["LATE_ENTRY_GATE_LEAD_H", "6.0", "Late-entry mode boundary"],
    ["LATE_ENTRY_MIN_EDGE", "20¢", "Required for late entry"],
    ["LATE_ENTRY_SIZE_MULT", "0.5", "Half-size late entries"],
    ["max_position_fraction", "0.025 (Plan B)", "% of bankroll per position"],
    ["min_position_notional", "$5 (Plan B)", "Minimum trade size"],
    ["max_correlation_group_fraction", "0.15", "Per-group exposure cap"],
    ["max_daily_drawdown", "0.14", "DD circuit breaker"],
    ["kelly_fraction (quarter)", "0.25", "Quarter-Kelly sizing"],
    ["σ floor ULTRA / REAL / REANALYSIS", "1.5 / 2.0 / 2.5°F", "Three-tier"],
    ["Profit-take threshold", "20¢", "Best_bid gain"],
    ["Rebalance edge-drop fresh / stale", "10¢ / 15¢", "Hash-change-aware"],
    ["Rebalance cooldown", "3 hours", "Post-exit lock"],
    ["Reliability Brier target", "0.12", "Best-city benchmark"],
    ["Reliability sample target", "50", "Confidence threshold"],
    ["Coherence min violation", "3¢", "Arbitrage threshold"],
    ["Claude approval threshold", "0.60", "Confidence required"],
    ["Claude API timeout", "15s", "Fail-open if exceeded"],
]

dials_table = Table(dials_data, colWidths=[2.5*inch, 1.4*inch, 2.4*inch])
dials_table.setStyle(TableStyle([
    ("BACKGROUND", (0, 0), (-1, 0), NAVY),
    ("TEXTCOLOR", (0, 0), (-1, 0), white),
    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
    ("FONTSIZE", (0, 0), (-1, -1), 8),
    ("FONTNAME", (0, 1), (0, -1), "Courier"),
    ("FONTNAME", (1, 1), (1, -1), "Helvetica-Bold"),
    ("TEXTCOLOR", (1, 1), (1, -1), NAVY),
    ("ALIGN", (1, 0), (1, -1), "CENTER"),
    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ("TOPPADDING", (0, 0), (-1, -1), 4),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [white, SOFT_GREY]),
    ("GRID", (0, 0), (-1, -1), 0.3, TABLE_BORDER),
]))
story.append(dials_table)

story.append(PageBreak())


# ===========================================================================
# THREE LOGICAL SHIFTS
# ===========================================================================
story.append(Paragraph("Strategy Evolution: Three Shifts", h1_style))
story.append(HRFlowable(width="100%", thickness=2, color=GOLD,
                         spaceBefore=0, spaceAfter=10))

story.append(Paragraph(
    "The strategy has gone through three philosophical evolutions in "
    "the past 72 hours, each in response to live data:",
    body_style,
))

# Shift 1
story.append(Paragraph("SHIFT 1 — CALIBRATION-FIRST → MARKET-AWARE (April 24)", h2_style))
story.append(Paragraph(
    "<b>Signal:</b> Walk-forward audit + external PDF review showed "
    "systematic 2-3× overconfidence on low-p brackets.",
    body_style,
))
story.append(Paragraph(
    "<b>Response:</b> Shipped 7 fixes: skew-normal shape guard, "
    "n&lt;30 sample floor, isotonic post-hoc calibration, reliability-"
    "weighted sizing, bucket-level EV auto-block, GEFS ensemble σ "
    "integration, season-specific calibration.",
    body_style,
))

# Shift 2
story.append(Paragraph("SHIFT 2 — DEFENSIVE THROTTLE / PLAN B (April 25)", h2_style))
story.append(Paragraph(
    "<b>Signal:</b> Cumulative −$687 drawdown despite Shift 1 fixes. "
    "Per-city audit: 7 European/SH cities at 0% win rate at n≥5.",
    body_style,
))
story.append(Paragraph(
    "<b>Response:</b> Blocked 7 more cities (now 11 total), added "
    "high-p artifact cap (p&gt;0.85 + edge&gt;0.20 reject), halved "
    "position sizing (5% → 2.5%, $10 → $5), reduced rebalance "
    "cooldown (6h → 3h), added rigorous P*_breakeven exit rule.",
    body_style,
))

# Shift 3
story.append(Paragraph("SHIFT 3 — EFFICIENT-MARKETS PARADIGM (April 25, post-Plan-B)", h2_style))
story.append(Paragraph(
    "<b>Signal:</b> User insight — \"maybe our assumption that our "
    "model is fully correct is the wrong assumption.\"",
    body_style,
))
story.append(Paragraph(
    "<b>Response:</b> Three new strategies replacing the \"raw model "
    "&gt; market = edge\" logic:",
    body_style,
))
story.append(Paragraph(
    "&nbsp;&nbsp;• <b>Strategy 1:</b> pure-arithmetic arbitrage (no model)<br/>"
    "&nbsp;&nbsp;• <b>Strategy 3:</b> ensemble-confidence-weighted blend<br/>"
    "&nbsp;&nbsp;• <b>Strategy 7:</b> Claude API qualitative gate",
    body_style,
))

story.append(Spacer(1, 0.15*inch))

story.append(Paragraph("CURRENT STATE", h3_style))
story.append(Paragraph(
    "The pipeline now has THREE distinct ways to find edge:",
    body_style,
))
story.append(Paragraph(
    "&nbsp;&nbsp;1. <b>Pure arithmetic</b> — markets are mostly right "
    "but sometimes mathematically inconsistent<br/>"
    "&nbsp;&nbsp;2. <b>Confident model + market disagreement</b> — when "
    "ensemble confirms, lean in (small)<br/>"
    "&nbsp;&nbsp;3. <b>Coherence + Claude veto</b> — qualitative "
    "artifact detection on what survives quant gates",
    body_style,
))

story.append(Spacer(1, 0.1*inch))
story.append(Paragraph("THE PARADIGM IN ONE SENTENCE", h3_style))
story.append(Paragraph(
    '"The market is the prior; our model is one piece of evidence; '
    'edge requires identifiable inefficiency, not raw disagreement."',
    callout_style,
))

story.append(PageBreak())


# ===========================================================================
# FINAL — WHAT TO EXPECT
# ===========================================================================
story.append(Paragraph("Forward Outlook", h1_style))
story.append(HRFlowable(width="100%", thickness=2, color=GOLD,
                         spaceBefore=0, spaceAfter=10))

story.append(Paragraph("EXPECTED BEHAVIOR — NEXT 24 HOURS", h2_style))

story.append(Paragraph(
    "<b>Volume drop:</b> 60-80% reduction in signal generation. Most "
    "of yesterday's losing trades would now hit "
    "<code>s3_low_model_confidence</code> or "
    "<code>s3_blended_edge_too_small</code>.",
    body_style,
))

story.append(Paragraph(
    "<b>Quality:</b> Trades that survive are confident, market-respectful, "
    "and arithmetically coherent.",
    body_style,
))

story.append(Paragraph(
    "<b>First Strategy 1 fires:</b> Should appear within hours. "
    "Polymarket low-volume brackets regularly have arithmetic violations.",
    body_style,
))

story.append(Paragraph(
    "<b>Claude gate:</b> Opt-in via ANTHROPIC_API_KEY. Default off; gate "
    "is no-op when API key absent.",
    body_style,
))

story.append(Spacer(1, 0.1*inch))

story.append(Paragraph("MEASUREMENT PLAN — NEXT 7 DAYS", h2_style))

story.append(Paragraph(
    "1. Track <b>per-strategy win rates</b> via <code>strategy_subtype</code> "
    "metadata — Strategy 1 (coherence) vs Strategy 3 (blend) vs "
    "standard.<br/>"
    "2. Monitor <b>gate_rejects histogram</b> per cycle to verify the "
    "blocklist + caps are functioning.<br/>"
    "3. Watch the <b>reliability multiplier</b> impact on trade size "
    "across cities.<br/>"
    "4. Track <b>Claude veto rate</b> if API key is set.<br/>"
    "5. Compute <b>realized vs blended_edge correlation</b> after ≥30 "
    "post-flip trades — if blended_edge predicts actual P&L, the "
    "calibration is working.",
    body_style,
))

story.append(Spacer(1, 0.1*inch))

story.append(Paragraph("DEFERRED WORK", h2_style))

story.append(Paragraph(
    "&nbsp;&nbsp;• <b>Strategy 2 (within-event cluster discrimination):</b> "
    "2-3 days, real edge in bracket-selection<br/>"
    "&nbsp;&nbsp;• <b>Strategy 4 (PMF-shape arbitrage):</b> 3-4 days, "
    "advanced<br/>"
    "&nbsp;&nbsp;• <b>Strategy 5 (lag-monitor-triggered re-scan):</b> "
    "3-4 days, requires daemon refactor<br/>"
    "&nbsp;&nbsp;• <b>Strategy 6 (price momentum):</b> 2 days, uses "
    "market_prices data we already collect<br/>"
    "&nbsp;&nbsp;• <b>Real station obs (KMA/JMA APIs):</b> per-API "
    "integration, weeks of work<br/>"
    "&nbsp;&nbsp;• <b>Nowcasting layer (T-6h → T-0h):</b> ~1 week to "
    "build properly",
    body_style,
))

story.append(Spacer(1, 0.15*inch))

story.append(Paragraph("VERDICT", h3_style))
story.append(Paragraph(
    "The system is no longer betting against market consensus. Every "
    "signal must clear arithmetic, ensemble-agreement, and qualitative "
    "checkpoints. Expected behavior: dramatically fewer trades, "
    "materially higher quality. The next two weeks of paper data will "
    "validate or refute the post-flip thesis.",
    verdict_style,
))

story.append(Spacer(1, 0.3*inch))

story.append(Paragraph(
    "─── END OF REPORT ───",
    ParagraphStyle("End", parent=body_style, alignment=TA_CENTER,
                    textColor=GOLD, fontName="Helvetica-Bold", fontSize=10),
))


# ===========================================================================
# BUILD
# ===========================================================================
doc.build(story)
print(f"Generated: {output_path}")

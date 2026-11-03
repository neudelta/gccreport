"""LTM perspective: brand palette and the GCC Origin offering the opportunity analysis maps to.

The service catalogue and engagement models are taken from LTM's GCC Origin launch release (28 Oct 2026).
Keep this file factual: the insights agent is told to use only these offerings when it recommends how LTM
can engage, so nothing here should be invented.
"""

# Brand palette (from ltm.com: black canvas, coral-red accent).
CORAL = "#F2553D"        # accent on dark backgrounds, chart highlight
CORAL_TEXT = "#C2391F"   # coral dark enough for text on white (WCAG AA)
INK = "#0B0B0C"          # near-black canvas
PANEL = "#17191E"        # dark panel
GREY_900 = "#1F2329"
GREY_600 = "#5B6470"
GREY_300 = "#D9DDE3"
GREY_100 = "#F3F4F6"
WHITE = "#FFFFFF"

BU_NAME = "GCC Origin"
PARENT_LINE = "A Larsen & Toubro Group Company"

SERVICES = {
    "Design & advisory": "GCC strategy, location and engagement model; operating blueprint, governance framework and "
                         "commercial/ownership model.",
    "Talent & workforce": "AI-driven hiring, talent management and upskilling (Talent Pool Radar, TalentInsights, "
                          "LTM Shoshin).",
    "AI agents & studios": "BlueVerse with 300+ pre-built domain AI agents and custom agents, AI Centres of Excellence, "
                           "innovation-as-a-service, the Automate suite (Agent Next) and AI studios in the US, Europe "
                           "and India.",
    "Operate & transform": "IT and business operations, transition, governance, knowledge management, finance, tax, "
                           "payroll and workplace services.",
    "Transfer": "Structured transfer of talent, technology, knowledge, infrastructure and operational ownership.",
    "Site & infrastructure": "Site selection, workplace and infrastructure, operational readiness and turnkey "
                             "facilities across Tier 1 and Tier 2 cities.",
}

ENGAGEMENT_MODELS = (
    "Assisted GCC",
    "Build-Operate-Transform-Transfer",
    "Virtual GCC",
    "Joint Venture",
    "Staff Augmentation",
    "Hire-Operate-Transfer",
)

MODEL_SHORT = {
    "Assisted GCC": "Assisted",
    "Build-Operate-Transform-Transfer": "BOTT",
    "Virtual GCC": "Virtual",
    "Joint Venture": "JV",
    "Staff Augmentation": "Staff aug.",
    "Hire-Operate-Transfer": "HOT",
}

CATALOGUE_TEXT = (
    f"LTM {BU_NAME} helps enterprises establish, operate, scale and transform GCCs in India, making them AI-native "
    "(powered by BlueVerse, LTM's agentic AI ecosystem). Services:\n"
    + "\n".join(f"- {k}: {v}" for k, v in SERVICES.items())
    + "\nEngagement models: " + ", ".join(ENGAGEMENT_MODELS) + "."
)

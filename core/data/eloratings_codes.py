"""eloratings.net 2-letter team code -> canonical team name.

Codes were VERIFIED against the live eloratings.net/World.tsv feed (Jun 2026)
by cross-referencing Elo values with known team rankings — not guessed from
ISO 3166-1, since eloratings uses football-specific codes that diverge for
the UK home nations (Scotland=SQ, Wales=WA, Northern Ireland=NI) and a few
others (Kosovo=KO, North Macedonia=NM).

Covers the WC 2026 field (48 teams, all explicitly verified) and the top
~50 international opponents most likely to appear in the last ~4 years of
historical results used to fit the Dixon-Coles strengths.

Unknown codes are skipped (logged once); the team simply falls back to the
neutral 1500 Elo baseline downstream, so a missing entry never crashes.
"""
from __future__ import annotations

EL_CODE_TO_TEAM: dict[str, str] = {
    # ---------------- WC 2026 hosts + qualifiers (48 teams, all verified) ----
    # Group A
    "MX": "Mexico", "ZA": "South Africa", "KR": "South Korea", "CZ": "Czechia",
    # Group B
    "CA": "Canada", "BA": "Bosnia-Herzegovina", "QA": "Qatar", "CH": "Switzerland",
    # Group C
    "BR": "Brazil", "MA": "Morocco", "HT": "Haiti", "SQ": "Scotland",
    # Group D
    "US": "United States", "PY": "Paraguay", "AU": "Australia", "TR": "Türkiye",
    # Group E
    "DE": "Germany", "CW": "Curacao", "CI": "Ivory Coast", "EC": "Ecuador",
    # Group F
    "NL": "Netherlands", "JP": "Japan", "SE": "Sweden", "TN": "Tunisia",
    # Group G
    "BE": "Belgium", "EG": "Egypt", "IR": "Iran", "NZ": "New Zealand",
    # Group H
    "ES": "Spain", "CV": "Cape Verde", "SA": "Saudi Arabia", "UY": "Uruguay",
    # Group I
    "FR": "France", "SN": "Senegal", "IQ": "Iraq", "NO": "Norway",
    # Group J
    "AR": "Argentina", "DZ": "Algeria", "AT": "Austria", "JO": "Jordan",
    # Group K
    "PT": "Portugal", "CD": "Congo DR", "UZ": "Uzbekistan", "CO": "Colombia",
    # Group L
    "EN": "England", "HR": "Croatia", "GH": "Ghana", "PA": "Panama",

    # ---------------- Frequent non-WC opponents (Elo-ranked top ~50) ---------
    # UEFA (deep field — most common opponents for European qualifiers)
    "IT": "Italy", "PL": "Poland", "DK": "Denmark", "RU": "Russia",
    "UA": "Ukraine", "GR": "Greece", "RS": "Serbia", "HU": "Hungary",
    "IE": "Ireland", "WA": "Wales", "SI": "Slovenia", "SK": "Slovakia",
    "GE": "Georgia", "IL": "Israel", "AL": "Albania", "RO": "Romania",
    "NM": "North Macedonia", "IS": "Iceland", "NI": "Northern Ireland",
    "FI": "Finland", "CY": "Cyprus", "BG": "Bulgaria", "BY": "Belarus",
    "ME": "Montenegro", "EE": "Estonia", "LV": "Latvia", "LT": "Lithuania",
    "MD": "Moldova", "AM": "Armenia", "AZ": "Azerbaijan", "KO": "Kosovo",
    # CONMEBOL non-WC
    "CL": "Chile", "PE": "Peru", "VE": "Venezuela", "BO": "Bolivia",
    # CONCACAF non-WC
    "CR": "Costa Rica", "JM": "Jamaica", "HN": "Honduras", "GT": "Guatemala",
    "TT": "Trinidad and Tobago", "SV": "El Salvador",
    # AFC non-WC
    "CN": "China", "KP": "North Korea", "TH": "Thailand", "VN": "Vietnam",
    "MY": "Malaysia", "ID": "Indonesia", "AE": "United Arab Emirates",
    "OM": "Oman", "KZ": "Kazakhstan",
    # CAF non-WC
    "NG": "Nigeria", "CM": "Cameroon", "ML": "Mali", "BF": "Burkina Faso",
    "AO": "Angola", "ZM": "Zambia", "ZW": "Zimbabwe", "KE": "Kenya",
    "UG": "Uganda", "MZ": "Mozambique", "GA": "Gabon",
}

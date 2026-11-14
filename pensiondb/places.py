"""Sponsor region (from a postcode) and sector (from a SIC code) for filtering.

region(postcode): the UK nation or English region (ITL1) of the postcode area, the letters at the start. A few areas
straddle a boundary (CH Chester and north-east Wales, SY Shropshire and mid Wales, KT and EN on the edge of London,
PE across the East and East Midlands); each is put where most of it lies.

sector(sic): the SIC 2007 section of the first SIC code, with holding companies and head offices (64201-64209, 70100)
split out, because large groups' parent companies use them and would otherwise swamp "Financial" and "Professional".
"""
import re

REGIONS = {
    "North East": "DH DL NE SR TS",
    "North West": "BB BL CA CH CW FY L LA M OL PR SK WA WN",
    "Yorkshire and the Humber": "BD DN HD HG HU HX LS S WF YO",
    "East Midlands": "DE LE LN NG NN",
    "West Midlands": "B CV DY HR ST SY TF WR WS WV",
    "East of England": "AL CB CM CO IP LU NR PE SG SS WD",
    "London": "E EC N NW SE SW W WC BR CR EN HA IG RM SM TW UB",
    "South East": "BN CT DA GU HP KT ME MK OX PO RG RH SL SO TN",
    "South West": "BA BH BS DT EX GL PL SN SP TA TQ TR",
    "Wales": "CF LD LL NP SA",
    "Scotland": "AB DD DG EH FK G HS IV KA KW KY ML PA PH TD ZE",
    "Northern Ireland": "BT",
    "Channel Islands and Isle of Man": "GY JE IM",
}
AREA = {a: r for r, areas in REGIONS.items() for a in areas.split()}
PC = re.compile(r"^([A-Z]{1,2})\d[A-Z\d]?\s*\d[A-Z]{2}$")

SECTIONS = [  # (first division, last division, label)
    (1, 3, "Agriculture, forestry and fishing"), (5, 9, "Mining and quarrying"), (10, 33, "Manufacturing"),
    (35, 35, "Energy supply"), (36, 39, "Water and waste"), (41, 43, "Construction"),
    (45, 47, "Wholesale, retail and motor trade"), (49, 53, "Transport and storage"),
    (55, 56, "Accommodation and food"), (58, 63, "Information and communication"),
    (64, 66, "Financial and insurance"), (68, 68, "Real estate"), (69, 75, "Professional, scientific and technical"),
    (77, 82, "Administrative and support services"), (84, 84, "Public administration and defence"),
    (85, 85, "Education"), (86, 88, "Health and social work"), (90, 93, "Arts, entertainment and recreation"),
    (94, 96, "Other services"), (97, 98, "Households as employers"), (99, 99, "Extraterritorial organisations"),
]
REGISTER_SECTOR = {"ccew": "Charity", "oscr": "Charity", "fca": "Mutual, co-operative or society",
                   "co": "Trade union or employers' association"}


def region(postcode):
    m = PC.match((postcode or "").strip().upper())
    if not m:
        return "Outside the UK or unknown" if postcode else None
    return AREA.get(m.group(1), "Outside the UK or unknown")


def sector(sic):
    m = re.match(r"\s*(\d{4,5})", sic or "")
    if not m:
        return None
    code = m.group(1).zfill(5)
    if code == "99999":
        return "Dormant company"
    if code == "70100" or "64201" <= code <= "64209":
        return "Holding company or head office"
    div = int(code[:2])
    return next((label for lo, hi, label in SECTIONS if lo <= div <= hi), None)

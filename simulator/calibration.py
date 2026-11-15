"""Every tuning number in one place.

SOURCED values come from the NIBSS figures quoted in the pitch deck (2026 Nigeria
Electronic Fraud Forum). ASSUMED values are placeholders chosen to look plausible;
they have not been checked against any data and should be replaced when real
figures are available.

Money is in naira here and converted to kobo where it is used.
"""

# --------------------------------------------------------------------------- SOURCED

# 47% of 2025 fraud volume came from social engineering.
SOCIAL_ENGINEERING_SHARE = 0.47

# Internet banking: N13.37bn lost across 4,507 cases, so about N2.97m per case.
WEB_FRAUD_MEAN_NAIRA = 13_370_000_000 / 4_507

# Internet banking's share of the N25.85bn total loss (about 52%).
WEB_FRAUD_LOSS_SHARE = 13.37 / 25.85

# --------------------------------------------------------------------------- ASSUMED

# Fraud amount per channel as (median naira, lognormal sigma). The web median is
# set so the mean lands near WEB_FRAUD_MEAN_NAIRA: mean = median * exp(sigma^2 / 2).
FRAUD_AMOUNT = {
    "web": (1_800_000, 1.0),
    "mobile": (250_000, 1.1),
    "ussd": (40_000, 0.9),
}
FRAUD_AMOUNT_MIN = 20_000
FRAUD_AMOUNT_MAX = 30_000_000

# Channel mix of non-insider theft cases. Insider cases always use web. The web
# weight is kept low so that web's share of total loss stays near the sourced 52%.
FRAUD_CHANNEL_WEIGHTS = {"web": 0.03, "mobile": 0.74, "ussd": 0.23}

# Share of fraud cases that start in the small hours (22:00 to 05:59).
FRAUD_ODD_HOUR_SHARE = 0.5

# Delay between a theft and the victim's report: median 45 minutes, 5 min to 24 h.
REPORT_DELAY_MEDIAN_S = 45 * 60
REPORT_DELAY_SIGMA = 1.2
REPORT_DELAY_MIN_S = 5 * 60
REPORT_DELAY_MAX_S = 24 * 3600

# Delay between a mule receiving money and moving it on: median 4 minutes.
HOP_DELAY_MEDIAN_S = 4 * 60
HOP_DELAY_SIGMA = 0.7
HOP_DELAY_MIN_S = 45
HOP_DELAY_MAX_S = 40 * 60

# Share of the money a mule keeps as commission at each hop.
MULE_KEEP_RANGE = (0.0, 0.08)

# Cash-out limits: one ATM withdrawal, one POS-agent cash-out, and the amount above
# which money leaves through a merchant (crypto or betting wallet) instead of cash.
ATM_MAX_NAIRA = 100_000
POS_AGENT_MAX_NAIRA = 150_000
CASH_EXIT_THRESHOLD_NAIRA = 300_000

# Mule rings.
RING_SIZE_RANGE = (7, 10)
RING_SHARED_DEVICES = 2
MULE_SHARED_DEVICE_SHARE = 0.8      # mules using one of the ring's shared devices
MULE_SHARED_BVN_SHARE = 0.2         # mules reusing another ring member's BVN
MULE_FINTECH_SHARE = 0.6            # mule accounts opened at fintechs
MULE_MAX_AGE_DAYS = 20              # opened at most this long before first use

# Customer segments: share of customers, opening balance (median naira, sigma),
# and average transactions per day.
SEGMENTS = {
    "salaried": {"share": 0.69, "balance": (150_000, 1.5), "rate": 1.4},
    "trader": {"share": 0.10, "balance": (400_000, 1.5), "rate": 3.0},
    "student": {"share": 0.20, "balance": (15_000, 1.2), "rate": 0.9},
    "dormant": {"share": 0.01, "balance": (1_500_000, 1.0), "rate": 0.0},
}
SECOND_ACCOUNT_SHARE = 0.10         # customers with an account at a second institution

# What an ordinary customer does, and through which channel.
NORMAL_KINDS = {"p2p": 0.35, "merchant": 0.30, "atm": 0.15, "pos_cashout": 0.20}
P2P_CHANNELS = {"mobile": 0.60, "ussd": 0.25, "web": 0.15}
MERCHANT_CHANNELS = {"mobile": 0.40, "pos": 0.40, "web": 0.20}
P2P_AMOUNT = (8_000, 1.1)
MERCHANT_AMOUNT = (5_000, 1.0)
SALARY_AMOUNT = (180_000, 0.6)
SUPPLIER_AMOUNT = (60_000, 0.8)

# Relative activity by hour of day, 00:00 to 23:00.
HOUR_WEIGHTS = [1, 1, 1, 1, 1, 2, 4, 7, 10, 12, 12, 12, 13, 12, 11, 11, 12, 13, 13, 11, 9, 6, 4, 2]
ODD_HOURS = [22, 23, 0, 1, 2, 3, 4, 5]

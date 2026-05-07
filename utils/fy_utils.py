from datetime import date

_MONTH_NAMES = {
    1: "1.January", 2: "2.February", 3: "3.March",
    4: "4.April", 5: "5.May", 6: "6.June",
    7: "7.July", 8: "8.August", 9: "9.September",
    10: "10.October", 11: "11.November", 12: "12.December",
}


def get_financial_year(d: date) -> str:
    """Return FY label like 'FY 25-26' for a given date."""
    start = d.year if d.month >= 4 else d.year - 1
    end = start + 1
    return f"FY {str(start)[-2:]}-{str(end)[-2:]}"


def get_month_folder(d: date) -> str:
    """Return folder name like '5.May'."""
    return _MONTH_NAMES[d.month]

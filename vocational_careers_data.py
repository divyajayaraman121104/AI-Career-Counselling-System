"""
vocational_careers_data.py

Modular data source for Vocational Careers, prepared for the Career Counselling
Tool. This module ONLY holds data + filtering/query helpers. It contains no
Streamlit UI code, no rendering logic, and does not touch any existing
Career Guidance module. Import from this file wherever vocational data or
filtering is needed.

Source: Vocational_Careers.pdf
"""

from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# 1. CAREER CATEGORIES
# Each category maps to a list of career/job titles as listed in the source
# document. Keys are used internally; a human-readable label is provided via
# CATEGORY_LABELS for display layers (kept separate so UI/i18n can format
# these independently, consistent with the format_func pattern used
# elsewhere in the app).
# ---------------------------------------------------------------------------

VOCATIONAL_CAREERS: Dict[str, List[str]] = {
    "construction_infrastructure": [
        "Mason", "Carpenter", "Plumber", "Electrician", "Welder", "Painter",
        "Bar Bender", "Scaffolder", "Construction Supervisor",
        "Interior Decorator", "Tile Setter", "Flooring Technician",
    ],
    "manufacturing_mechanical": [
        "Fitter", "Turner", "Machinist", "CNC Operator", "CNC Programmer",
        "Tool & Die Maker", "Lathe Operator", "Machine Operator",
        "Sheet Metal Worker", "Foundry Technician",
    ],
    "automobile": [
        "Automobile Mechanic", "Two-Wheeler Mechanic", "Four-Wheeler Mechanic",
        "Diesel Mechanic", "Auto Electrician", "Service Technician",
        "Vehicle Inspector",
    ],
    "electrical_electronics": [
        "Wireman", "Electrical Technician", "Electronics Mechanic",
        "Solar PV Installer", "CCTV Installation Technician",
        "Home Appliance Repair Technician", "UPS Technician",
        "Inverter Technician",
    ],
    "information_technology": [
        "Computer Operator & Programming Assistant (COPA)",
        "Data Entry Operator", "Hardware Technician", "Network Technician",
        "Computer Service Technician", "Digital Marketing Executive",
        "Web Designer", "Graphic Designer",
    ],
    "healthcare": [
        "General Duty Assistant", "Nursing Assistant", "Lab Technician",
        "ECG Technician", "Phlebotomy Technician",
        "Medical Equipment Technician", "Home Health Aide",
        "Emergency Medical Technician",
    ],
    "hospitality_tourism": [
        "Cook", "Chef Assistant", "Baker", "Food Production Assistant",
        "Housekeeping Staff", "Front Office Executive", "Waiter",
        "Bartender", "Tour Guide",
    ],
    "beauty_wellness": [
        "Beautician", "Hair Stylist", "Makeup Artist", "Nail Technician",
        "Spa Therapist", "Massage Therapist", "Cosmetologist",
    ],
    "textile_fashion": [
        "Tailor", "Fashion Designer", "Embroidery Technician",
        "Sewing Machine Operator", "Textile Technician", "Boutique Assistant",
    ],
    "agriculture": [
        "Organic Farmer", "Dairy Farmer", "Poultry Farmer",
        "Horticulture Technician", "Floriculture Technician",
        "Irrigation Technician", "Farm Machinery Operator",
    ],
    "food_processing": [
        "Food Processing Technician", "Dairy Processing Technician",
        "Bakery Technician", "Packaging Technician",
        "Quality Control Assistant",
    ],
    "media_creative": [
        "Photographer", "Videographer", "Video Editor", "Animator",
        "Multimedia Technician", "Printing Technician",
    ],
    "logistics_retail": [
        "Warehouse Assistant", "Store Keeper", "Inventory Executive",
        "Retail Sales Associate", "Supply Chain Assistant",
        "Delivery Executive",
    ],
    "renewable_energy": [
        "Solar Technician", "Wind Turbine Technician", "Energy Auditor",
        "EV Charging Technician",
    ],
}

CATEGORY_LABELS: Dict[str, str] = {
    "construction_infrastructure": "Construction & Infrastructure",
    "manufacturing_mechanical": "Manufacturing & Mechanical",
    "automobile": "Automobile",
    "electrical_electronics": "Electrical & Electronics",
    "information_technology": "Information Technology",
    "healthcare": "Healthcare",
    "hospitality_tourism": "Hospitality & Tourism",
    "beauty_wellness": "Beauty & Wellness",
    "textile_fashion": "Textile & Fashion",
    "agriculture": "Agriculture",
    "food_processing": "Food Processing",
    "media_creative": "Media & Creative",
    "logistics_retail": "Logistics & Retail",
    "renewable_energy": "Renewable Energy",
}


# ---------------------------------------------------------------------------
# 2. ELIGIBILITY (general program-level info; not per-career in source doc)
# ---------------------------------------------------------------------------

ELIGIBILITY_INFO: Dict[str, object] = {
    "minimum_qualification_options": [
        "8th Pass (for some trades)",
        "10th Pass (most courses)",
        "12th Pass or ITI qualification (advanced vocational courses)",
        "10th or 12th Pass (Diploma/Polytechnic courses)",
    ],
    "age_range": {"min": 14, "max": 45},
    "age_note": "Varies by course and institution",
}


# ---------------------------------------------------------------------------
# 3. SKILLS
# ---------------------------------------------------------------------------

TECHNICAL_SKILLS: List[str] = [
    "Practical and hands-on skills", "Equipment handling", "Tool operation",
    "Technical knowledge", "Problem-solving", "Safety practices",
    "Quality control",
]

SOFT_SKILLS: List[str] = [
    "Communication", "Teamwork", "Time management", "Customer service",
    "Adaptability", "Professional ethics", "Critical thinking",
]


# ---------------------------------------------------------------------------
# 4. TRAINING INSTITUTES & COURSE INFO
# ---------------------------------------------------------------------------

TRAINING_INSTITUTES: List[str] = [
    "Industrial Training Institutes (ITI)",
    "National Skill Training Institutes (NSTI)",
    "Polytechnic Colleges",
    "NSDC Training Centers",
    "PMKVY Training Centers",
    "Skill India Programs",
    "State Skill Development Missions",
    "Private Skill Training Institutes",
    "Apprenticeship Programs",
    "Government ITIs",
    "Private ITIs",
    "Community Colleges",
    "Industrial Apprenticeship Training Centers",
]

COURSE_DURATION: Dict[str, str] = {
    "min": "3 months",
    "max": "2 years",
    "note": "Depends on the trade",
}

CERTIFICATIONS: List[str] = [
    "NCVT Certificate", "SCVT Certificate", "NSDC Certification",
    "Skill India Certification", "PMKVY Certification",
    "Apprenticeship Certificate", "Diploma Certificate",
    "Polytechnic Certificate",
]


# ---------------------------------------------------------------------------
# 5. SALARY & CAREER GROWTH
# ---------------------------------------------------------------------------

SALARY_BANDS: List[Dict[str, str]] = [
    {"experience": "Fresher", "salary_range": "₹12,000 – ₹20,000/month"},
    {"experience": "2–5 Years", "salary_range": "₹20,000 – ₹40,000/month"},
    {"experience": "Experienced", "salary_range": "₹40,000 – ₹80,000+/month"},
    {"experience": "Self-employed", "salary_range": "Varies based on business and clients"},
]

CAREER_GROWTH_PATH: List[str] = [
    "Trainee", "Technician", "Senior Technician", "Supervisor",
    "Team Leader", "Manager", "Entrepreneur/Business Owner",
]

EMPLOYMENT_SECTORS: List[str] = [
    "Government Departments", "Private Companies", "Manufacturing Industries",
    "Construction Companies", "Hospitals", "Hotels", "Automotive Industries",
    "IT Companies", "Retail Sector", "MSMEs", "Self-Employment",
]


# ---------------------------------------------------------------------------
# 6. LOCATIONS
# ---------------------------------------------------------------------------

TRAINING_EMPLOYMENT_CITIES: List[str] = [
    "Chennai", "Madurai", "Coimbatore", "Tiruchirappalli", "Salem",
    "Tirunelveli", "Bengaluru", "Mysuru", "Hyderabad", "Visakhapatnam",
    "Vijayawada", "Kochi", "Thiruvananthapuram", "Kozhikode", "Mumbai",
    "Pune", "Nagpur", "Nashik", "Ahmedabad", "Surat", "Vadodara", "Jaipur",
    "Udaipur", "Jodhpur", "New Delhi", "Noida", "Gurugram", "Chandigarh",
    "Lucknow", "Kanpur", "Bhopal", "Indore", "Raipur", "Bhubaneswar",
    "Kolkata", "Guwahati", "Ranchi", "Patna", "Srinagar", "Jammu",
]


# ---------------------------------------------------------------------------
# 7. RESOURCES
# ---------------------------------------------------------------------------

GOVERNMENT_PORTALS: List[str] = [
    "Skill India", "National Skill Development Corporation (NSDC)",
    "Bharat Skills", "Directorate General of Training (DGT)", "NCVT MIS",
    "Apprenticeship India", "Pradhan Mantri Kaushal Vikas Yojana (PMKVY)",
    "State Skill Development Missions",
]

LEARNING_RESOURCES: List[str] = [
    "ITI Training Materials", "Bharat Skills e-Learning",
    "Apprenticeship Programs", "Government Skill Development Schemes",
    "Industry Certification Programs", "Online Skill Development Courses",
]


# ---------------------------------------------------------------------------
# 8. FILTER / QUERY HELPERS
# These are pure functions (no Streamlit, no I/O) so they can be unit tested
# and reused by any future UI layer without modification.
# ---------------------------------------------------------------------------

def get_all_categories() -> List[str]:
    """Return internal category keys."""
    return list(VOCATIONAL_CAREERS.keys())


def get_category_label(category_key: str) -> str:
    """Return the human-readable label for a category key."""
    return CATEGORY_LABELS.get(category_key, category_key)


def get_careers_by_category(category_key: str) -> List[str]:
    """Return the list of careers for a given category key."""
    return VOCATIONAL_CAREERS.get(category_key, [])


def get_all_careers_flat() -> List[Dict[str, str]]:
    """
    Return every career as a flat list of dicts:
    [{"category": <key>, "category_label": <label>, "career": <name>}, ...]
    Useful for building a single filterable table/dataframe.
    """
    flat: List[Dict[str, str]] = []
    for category_key, careers in VOCATIONAL_CAREERS.items():
        label = get_category_label(category_key)
        for career in careers:
            flat.append({
                "category": category_key,
                "category_label": label,
                "career": career,
            })
    return flat


def search_careers(query: str) -> List[Dict[str, str]]:
    """
    Case-insensitive substring search across all careers.
    Returns matching entries in the same shape as get_all_careers_flat().
    """
    if not query:
        return get_all_careers_flat()
    query_lower = query.strip().lower()
    return [
        entry for entry in get_all_careers_flat()
        if query_lower in entry["career"].lower()
    ]


def filter_careers(
    categories: Optional[List[str]] = None,
    search_query: Optional[str] = None,
) -> List[Dict[str, str]]:
    """
    Combined filter: narrow by one or more category keys and/or a search
    term. Passing None/empty for a parameter skips that filter.
    """
    results = get_all_careers_flat()

    if categories:
        category_set = set(categories)
        results = [r for r in results if r["category"] in category_set]

    if search_query:
        query_lower = search_query.strip().lower()
        results = [r for r in results if query_lower in r["career"].lower()]

    return results


def get_category_choices_for_widget() -> List[str]:
    """
    Return category keys sorted by their display label — intended for use
    with Streamlit's `format_func=get_category_label` pattern, mirroring
    the approach used elsewhere in the app (original keys preserved for
    logic, translated/display label shown to the user).
    """
    return sorted(VOCATIONAL_CAREERS.keys(), key=get_category_label)


def get_total_career_count() -> int:
    """Return the total number of individual careers across all categories."""
    return sum(len(v) for v in VOCATIONAL_CAREERS.values())


def get_career_detail_context(career_name: str) -> Optional[Dict[str, object]]:
    """
    Return ONLY the data relevant to a single, specifically selected career
    - never the full VOCATIONAL_CAREERS dataset. This is the sole entry
    point any AI-integration layer should use to build the context it
    sends to an LLM for one career, so a request about one career never
    leaks the other 100+ careers (or the rest of the document) into the
    prompt.

    Matches the selected career case-insensitively against every career
    across every category (get_all_careers_flat()) and, if found, returns
    a compact dict containing:
      - the career's own name + its single category/category_label
      - the general (program-level, not per-career in the source doc)
        eligibility, skills, training, certification, salary, growth-path,
        employment-sector, and government-portal reference data

    Returns None if career_name doesn't match any known career.
    """
    if not career_name:
        return None

    query = career_name.strip().lower()
    match = next(
        (entry for entry in get_all_careers_flat() if entry["career"].lower() == query),
        None,
    )
    if match is None:
        return None

    return {
        "career": match["career"],
        "category": match["category"],
        "category_label": match["category_label"],
        "eligibility_options": ELIGIBILITY_INFO["minimum_qualification_options"],
        "age_range": ELIGIBILITY_INFO["age_range"],
        "age_note": ELIGIBILITY_INFO["age_note"],
        "technical_skills": TECHNICAL_SKILLS,
        "soft_skills": SOFT_SKILLS,
        "training_institutes": TRAINING_INSTITUTES,
        "course_duration": COURSE_DURATION,
        "certifications": CERTIFICATIONS,
        "salary_bands": SALARY_BANDS,
        "career_growth_path": CAREER_GROWTH_PATH,
        "employment_sectors": EMPLOYMENT_SECTORS,
        "government_portals": GOVERNMENT_PORTALS,
    }
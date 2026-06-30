import streamlit as st
import json
from pathlib import Path
from datetime import datetime
import base64
from fpdf import FPDF
import tempfile
import os
from google import genai
from google.genai import types


# Page configuration
st.set_page_config(
    page_title="CoActions - Career Guidance",
    page_icon="🎯",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# Custom CSS for Professional App with Background Image
st.markdown("""
<style>

    /* Background Image with Overlay */
    .stApp {
        background-image: url("https://images.unsplash.com/photo-1557683304-673a230ec87c?q=80&w=2029&auto=format&fit=crop");
        background-size: cover;
        background-position: center;
        background-attachment: fixed;
        background-repeat: no-repeat;
    }
    
    /* Dark Overlay for better readability */
    .stApp::before {
        content: "";
        position: fixed;
        top: 0;
        left: 0;
        right: 0;
        bottom: 0;
        background: linear-gradient(135deg, rgba(0,0,0,0.5), rgba(0,0,0,0.3));
        z-index: -1;
    }
    
    /* Main Card - Glassmorphism Effect */
    .main-card {
        background: rgba(255, 255, 255, 0.94);
        backdrop-filter: blur(12px);
        border-radius: 28px;
        padding: 2rem;
        box-shadow: 0 25px 50px -12px rgba(0,0,0,0.3);
        border: 1px solid rgba(255,255,255,0.3);
        margin-bottom: 1rem;
    }
    
    /* App Title with Gradient */
    .app-title {
        font-size: 2.5rem;
        font-weight: 800;
        background: linear-gradient(135deg, #667eea, #764ba2);
        -webkit-background-clip: text;
        background-clip: text;
        color: transparent;
    }
    
    /* Welcome Heading */
    .welcome-heading {
        background: linear-gradient(135deg, #667eea, #764ba2);
        -webkit-background-clip: text;
        background-clip: text;
        color: transparent;
        font-size: 2rem;
        font-weight: 700;
        margin-bottom: 0.5rem;
        text-align: center;
    }
    
    /* User Cards - Frosted Glass */
    .user-card {
        background: rgba(255, 255, 255, 0.85);
        backdrop-filter: blur(8px);
        border-radius: 24px;
        padding: 1.8rem;
        text-align: center;
        transition: all 0.3s ease;
        border: 2px solid rgba(102, 126, 234, 0.3);
        margin: 0.5rem;
        cursor: pointer;
    }
    .user-card:hover {
        transform: translateY(-6px);
        border-color: #667eea;
        background: rgba(255, 255, 255, 0.95);
        box-shadow: 0 20px 40px -12px rgba(102,126,234,0.4);
    }
    
    /* Modern Buttons */
    .stButton > button {
        background: linear-gradient(135deg, #667eea, #764ba2);
        color: white;
        border: none;
        border-radius: 40px;
        padding: 0.7rem 1.8rem;
        font-weight: 600;
        transition: all 0.3s ease;
        width: 100%;
    }
    .stButton > button:hover {
        background: linear-gradient(135deg, #5a67d8, #6b46c1);
        transform: translateY(-2px);
        box-shadow: 0 10px 20px -5px rgba(102,126,234,0.4);
    }
    
    /* Menu Buttons */
    .menu-btn {
        background: rgba(255,255,255,0.2) !important;
        backdrop-filter: blur(4px);
        border: 1px solid rgba(255,255,255,0.3) !important;
        color: white !important;
        padding: 0.5rem 1.2rem !important;
        border-radius: 40px !important;
        font-weight: 600 !important;
        font-size: 0.9rem !important;
        transition: all 0.3s ease !important;
    }
    .menu-btn:hover {
        background: rgba(102, 126, 234, 0.3) !important;
        color: #667eea !important;
        border-color: #667eea !important;
    }
    
    /* Question Cards */
    .question-card {
        background: rgba(247, 250, 252, 0.9);
        border-radius: 20px;
        padding: 1rem;
        margin: 0.8rem 0;
        border-left: 5px solid #667eea;
        backdrop-filter: blur(4px);
    }
    
    /* Stream Cards - Gradient Cards */
    .stream-card-high {
        background: linear-gradient(135deg, #11998e, #38ef7d);
        border-radius: 20px;
        padding: 1.5rem;
        text-align: center;
        transition: all 0.3s ease;
        margin: 0.5rem;
        cursor: pointer;
        color: white;
    }
    .stream-card-good {
        background: linear-gradient(135deg, #f2994a, #f2c94c);
        border-radius: 20px;
        padding: 1.5rem;
        text-align: center;
        transition: all 0.3s ease;
        margin: 0.5rem;
        cursor: pointer;
        color: white;
    }
    .stream-card-fair {
        background: linear-gradient(135deg, #ff6b6b, #feca57);
        border-radius: 20px;
        padding: 1.5rem;
        text-align: center;
        transition: all 0.3s ease;
        margin: 0.5rem;
        cursor: pointer;
        color: white;
    }
    .stream-card-potential {
        background: linear-gradient(135deg, #4facfe, #00f2fe);
        border-radius: 20px;
        padding: 1.5rem;
        text-align: center;
        transition: all 0.3s ease;
        margin: 0.5rem;
        cursor: pointer;
        color: white;
    }
    .stream-card-low {
        background: linear-gradient(135deg, #8e9eab, #eef2f3);
        border-radius: 20px;
        padding: 1.5rem;
        text-align: center;
        transition: all 0.3s ease;
        margin: 0.5rem;
        cursor: pointer;
        color: #4a5568;
    }
    
    .stream-card-high:hover, .stream-card-good:hover, 
    .stream-card-fair:hover, .stream-card-potential:hover, 
    .stream-card-low:hover {
        transform: translateY(-8px);
        box-shadow: 0 20px 40px -12px rgba(0,0,0,0.3);
    }
    
    /* Score Bar */
    .score-bar {
        background: rgba(0,0,0,0.2);
        border-radius: 20px;
        height: 10px;
        margin: 0.5rem 0;
        overflow: hidden;
    }
    .score-fill {
        background: linear-gradient(90deg, #11998e, #38ef7d);
        border-radius: 20px;
        height: 100%;
        transition: width 1s ease;
    }
    
    /* Match Card */
    .match-high {
        background: linear-gradient(135deg, rgba(232,245,233,0.9), rgba(200,230,201,0.9));
        backdrop-filter: blur(4px);
        border-left: 8px solid #11998e;
        border-radius: 20px;
        padding: 1.5rem;
        margin: 1rem 0;
    }
    
    /* Radio Buttons */
    .stRadio > div {
        background: rgba(255,255,255,0.9);
        padding: 0.8rem;
        border-radius: 20px;
    }
    
    /* Progress Bar */
    .progress-text {
        color: #667eea;
        font-size: 0.85rem;
        margin-top: 0.5rem;
        font-weight: 600;
    }
    
    /* Page Counter */
    .page-counter {
        background: linear-gradient(135deg, #667eea, #764ba2);
        border-radius: 20px;
        padding: 0.5rem 1rem;
        text-align: center;
        margin: 1rem 0;
        color: white;
        font-weight: 600;
    }
    
    /* User Type Indicator */
    .user-type-indicator {
        background: linear-gradient(135deg, #667eea, #764ba2);
        color: white;
        padding: 0.3rem 1rem;
        border-radius: 20px;
        display: inline-block;
        margin-bottom: 1rem;
        font-weight: 600;
    }
    
    /* Scrollbar */
    ::-webkit-scrollbar {
        width: 8px;
        height: 8px;
    }
    ::-webkit-scrollbar-track {
        background: rgba(255,255,255,0.1);
        border-radius: 10px;
    }
    ::-webkit-scrollbar-thumb {
        background: linear-gradient(135deg, #667eea, #764ba2);
        border-radius: 10px;
    }
    
    /* Print styles */
    @media print {
        .stButton, .stDownloadButton, .menu-container {
            display: none !important;
        }
        .main-card {
            background: white !important;
            box-shadow: none !important;
        }
        .stApp::before {
            display: none !important;
        }
    }

    /* ==================== FIXED DROPDOWN VISIBILITY - CRITICAL FIX ==================== */
    
    /* SelectBox Container - Base styling */
    .stSelectbox > div {
        background: white !important;
        border-radius: 12px !important;
    }
    
    /* The main select box input field */
    .stSelectbox div[data-baseweb="select"] {
        background: white !important;
        border: 2px solid #1E88E5 !important;
        border-radius: 12px !important;
        min-height: 45px !important;
    }
    
    .stSelectbox div[data-baseweb="select"]:hover {
        border-color: #1565C0 !important;
        background: #E3F2FD !important;
    }
    
    /* THE MOST IMPORTANT FIX - Selected value text */
    .stSelectbox div[data-baseweb="select"] div {
        color: #1a1a2e !important;
        font-size: 1rem !important;
        font-weight: 500 !important;
    }
    
    /* The value display span */
    .stSelectbox div[data-baseweb="select"] span {
        color: #1a1a2e !important;
        font-weight: 500 !important;
    }
    
    /* Dropdown arrow icon */
    .stSelectbox svg {
        fill: #1E88E5 !important;
    }
    
    /* Dropdown menu container (when opened) */
    div[data-baseweb="popover"] {
        background: white !important;
        border: 1px solid #1E88E5 !important;
        border-radius: 12px !important;
        box-shadow: 0 4px 12px rgba(0,0,0,0.15) !important;
    }
    
    /* Dropdown options list */
    ul[role="listbox"] {
        background: white !important;
        border-radius: 12px !important;
    }
    
    /* Individual option items */
    li[role="option"] {
        color: #1a1a2e !important;
        background: white !important;
        padding: 10px 15px !important;
        font-size: 0.9rem !important;
    }
    
    /* Hover effect on options */
    li[role="option"]:hover {
        background: #E3F2FD !important;
        color: #1E88E5 !important;
    }
    
    /* Selected option in dropdown */
    li[role="option"][aria-selected="true"] {
        background: #1E88E5 !important;
        color: white !important;
    }
    
    /* ==================== INPUT FIELDS STYLING ==================== */
    
    /* Text Input Fields */
    .stTextInput > div > div > input {
        background: white !important;
        border: 2px solid #1E88E5 !important;
        border-radius: 12px !important;
        padding: 12px 15px !important;
        font-size: 1rem !important;
        color: #1a1a2e !important;
        transition: all 0.3s ease !important;
    }
    
    .stTextInput > div > div > input:focus {
        border-color: #1565C0 !important;
        box-shadow: 0 0 0 3px rgba(30, 136, 229, 0.2) !important;
        outline: none !important;
    }
    
    .stTextInput > div > div > input:hover {
        border-color: #1565C0 !important;
        background: #E3F2FD !important;
    }
    
    /* Number Input Fields */
    .stNumberInput > div > div > input {
        background: white !important;
        border: 2px solid #1E88E5 !important;
        border-radius: 12px !important;
        padding: 12px 15px !important;
        font-size: 1rem !important;
        color: #1a1a2e !important;
        transition: all 0.3s ease !important;
    }
    
    .stNumberInput > div > div > input:focus {
        border-color: #1565C0 !important;
        box-shadow: 0 0 0 3px rgba(30, 136, 229, 0.2) !important;
        outline: none !important;
    }
    
    /* Labels */
    .stTextInput label, 
    .stNumberInput label, 
    .stSelectbox label {
        color: #1565C0 !important;
        font-weight: 600 !important;
        font-size: 0.9rem !important;
        margin-bottom: 5px !important;
    }
    
    /* Placeholder text */
    .stTextInput input::placeholder,
    .stNumberInput input::placeholder {
        color: #90A4AE !important;
        font-size: 0.9rem !important;
    }
       
</style>
""", unsafe_allow_html=True)

# Initialize session state
if 'page' not in st.session_state:
    st.session_state.page = 'welcome'
if 'user_type' not in st.session_state:
    st.session_state.user_type = None
if 'student_city' not in st.session_state:
    st.session_state.student_city = ""
if 'student_state' not in st.session_state:
    st.session_state.student_state = ""
if 'responses' not in st.session_state:
    st.session_state.responses = {}
if 'current_page' not in st.session_state:
    st.session_state.current_page = 0
if 'questions_list' not in st.session_state:
    st.session_state.questions_list = []
if 'categories_data' not in st.session_state:
    st.session_state.categories_data = {}
if 'recommended_categories' not in st.session_state:
    st.session_state.recommended_categories = []
if 'selected_stream' not in st.session_state:
    st.session_state.selected_stream = None
if 'selected_stream_data' not in st.session_state:
    st.session_state.selected_stream_data = None
if 'student_name' not in st.session_state:
    st.session_state.student_name = ""
if 'student_age' not in st.session_state:
    st.session_state.student_age = ""
if 'student_institution' not in st.session_state:
    st.session_state.student_institution = ""
if 'student_grade' not in st.session_state:
    st.session_state.student_grade = ""
if 'show_about' not in st.session_state:
    st.session_state.show_about = False
if 'show_contact' not in st.session_state:
    st.session_state.show_contact = False
if 'selected_career' not in st.session_state:
    st.session_state.selected_career = None
if 'show_career_detail' not in st.session_state:
    st.session_state.show_career_detail = False
if 'personality_responses' not in st.session_state:
    st.session_state.personality_responses = {}
if 'personality_current_index' not in st.session_state:
    st.session_state.personality_current_index = 0
if 'personality_questions' not in st.session_state:
    st.session_state.personality_questions = []
if 'personality_completed' not in st.session_state:
    st.session_state.personality_completed = False
if 'personality_pathway' not in st.session_state:
    st.session_state.personality_pathway = None
if 'ai_recommendation' not in st.session_state:
    st.session_state.ai_recommendation = None
if 'ai_top_streams' not in st.session_state:
    st.session_state.ai_top_streams = None
if 'ai_analysis' not in st.session_state:
    st.session_state.ai_analysis = None
if 'ai_analysis_status' not in st.session_state:
    st.session_state.ai_analysis_status = None
if 'ai_deep_dive' not in st.session_state:
    st.session_state.ai_deep_dive = None
if 'ai_deep_dive_status' not in st.session_state:
    st.session_state.ai_deep_dive_status = None
if 'selected_career_role' not in st.session_state:
    st.session_state.selected_career_role = None
if 'ai_role_detail' not in st.session_state:
    st.session_state.ai_role_detail = None
if 'ai_role_detail_status' not in st.session_state:
    st.session_state.ai_role_detail_status = None
# Fingerprints used to detect whether the underlying questionnaire/personality
# answers have changed - Gemini is only re-called when the relevant
# fingerprint no longer matches the cached one (caching layer below).
if 'ai_recommendation_fingerprint' not in st.session_state:
    st.session_state.ai_recommendation_fingerprint = None
if 'ai_analysis_fingerprint' not in st.session_state:
    st.session_state.ai_analysis_fingerprint = None
if 'ai_deep_dive_fingerprint' not in st.session_state:
    st.session_state.ai_deep_dive_fingerprint = None
if 'ai_role_detail_fingerprint' not in st.session_state:
    st.session_state.ai_role_detail_fingerprint = None
if 'gemini_response_raw' not in st.session_state:
    st.session_state.gemini_response_raw = None
if 'gemini_response_error' not in st.session_state:
    st.session_state.gemini_response_error = None
# Raw technical error/debug text for the other three Gemini calls, kept
# separate from the user-facing 'message' in *_status so the UI can show a
# friendly message while still offering the technical details inside an
# expandable debug section (never inline in the friendly message itself).
if 'ai_analysis_error' not in st.session_state:
    st.session_state.ai_analysis_error = None
if 'ai_deep_dive_error' not in st.session_state:
    st.session_state.ai_deep_dive_error = None
if 'ai_role_detail_error' not in st.session_state:
    st.session_state.ai_role_detail_error = None

# Load JSON files
@st.cache_data
def load_json_file(file_path):
    """Load JSON file with error handling"""
    try:
        full_path = Path(file_path)
        if not full_path.exists():
            st.error(f"File not found: {file_path}")
            return None
        with open(full_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        # Debug info
        total_questions = 0
        for cat in data.get('categories', []):
            # Check for both formats
            if 'questions' in cat:
                total_questions += len(cat['questions'])
            else:
                total_questions += len(cat.get('parent_questions', []))
                total_questions += len(cat.get('subfield_questions', []))
        
        print(f"Loaded {file_path}: {len(data.get('categories', []))} categories, {total_questions} questions")
        return data
    except Exception as e:
        st.error(f"Error loading {file_path}: {str(e)}")
        return None
        
def extract_questions_from_json(data):
    """Extract questions and categories from JSON format"""
    questions = []
    categories = {}
    
    if not data or 'categories' not in data:
        print("No categories found in data")
        return questions, categories
    
    for category in data.get('categories', []):
        cat_id = category.get('category_id')
        cat_name = category.get('category_name')
        cat_icon = category.get('icon', '📁')
        cat_color = category.get('color', '#6B7280')
        
        categories[cat_id] = {
            'id': cat_id,
            'name': cat_name,
            'icon': cat_icon,
            'color': cat_color,
            'score': 0,
            'question_count': 0,
            'subfield_scores': {}
        }
        
        # IMPORTANT: Use 'questions' key (not 'parent_questions' or 'subfield_questions')
        # This works for both school.json and college.json
        category_questions = category.get('questions', [])
        
        # Also handle legacy format (parent_questions + subfield_questions)
        if not category_questions:
            # Legacy format: combine parent and subfield questions
            parent_qs = category.get('parent_questions', [])
            subfield_qs = category.get('subfield_questions', [])
            category_questions = parent_qs + subfield_qs
        
        print(f"Loading {len(category_questions)} questions for {cat_name}")
        
        for q in category_questions:
            q_id = q.get('id')
            q_text = q.get('text')
            q_weight = q.get('weight', 1.0)
            q_type = q.get('type', 'parent')
            q_subfield = q.get('subfield')
            
            # Skip if missing required fields
            if not q_id or not q_text:
                print(f"Warning: Question missing id or text in {cat_name}")
                continue
            
            # Add to questions list
            questions.append({
                'id': q_id,
                'text': q_text,
                'category_id': cat_id,
                'category_name': cat_name,
                'weight': q_weight,
                'type': q_type,
                'subfield': q_subfield
            })
            
            # Initialize subfield tracking if needed
            if q_subfield and q_subfield != 'null' and q_subfield is not None:
                if q_subfield not in categories[cat_id]['subfield_scores']:
                    categories[cat_id]['subfield_scores'][q_subfield] = {
                        'score': 0,
                        'max_score': 0,
                        'count': 0
                    }
            
            # Increment question count
            categories[cat_id]['question_count'] += 1
    
    print(f"Total questions extracted: {len(questions)}")
    print(f"Total categories: {len(categories)}")
    
    return questions, categories

def get_personality_questions(user_type):
    """Get 25 personality questions based on user type"""
    school_questions = [
        {"id": 1, "text": "Do you enjoy solving puzzles or math problems in your free time?", "options": ["Yes, a lot", "Sometimes", "Not really"]},
        {"id": 2, "text": "Do you like drawing, painting, or crafting things?", "options": ["Yes, a lot", "Sometimes", "Not really"]},
        {"id": 3, "text": "Are you interested in how computers and games work?", "options": ["Yes, a lot", "Sometimes", "Not really"]},
        {"id": 4, "text": "Do you enjoy reading storybooks or writing small stories?", "options": ["Yes, a lot", "Sometimes", "Not really"]},
        {"id": 5, "text": "Do you like helping classmates with their work?", "options": ["Yes, a lot", "Sometimes", "Not really"]},
        {"id": 6, "text": "Are you curious about stars, planets, or science experiments?", "options": ["Yes, a lot", "Sometimes", "Not really"]},
        {"id": 7, "text": "Do you enjoy organizing events or leading a group?", "options": ["Yes, a lot", "Sometimes", "Not really"]},
        {"id": 8, "text": "Do you like playing sports or outdoor games?", "options": ["Yes, a lot", "Sometimes", "Not really"]},
        {"id": 9, "text": "Do you enjoy listening to music or playing an instrument?", "options": ["Yes, a lot", "Sometimes", "Not really"]},
        {"id": 10, "text": "Do you like fixing broken toys or gadgets?", "options": ["Yes, a lot", "Sometimes", "Not really"]},
        {"id": 11, "text": "Do you enjoy debating or discussing topics with friends?", "options": ["Yes, a lot", "Sometimes", "Not really"]},
        {"id": 12, "text": "Do you like memorizing facts or learning new words?", "options": ["Yes, a lot", "Sometimes", "Not really"]},
        {"id": 13, "text": "Do you enjoy gardening or taking care of pets?", "options": ["Yes, a lot", "Sometimes", "Not really"]},
        {"id": 14, "text": "Do you prefer working alone rather than in a group?", "options": ["Yes, a lot", "Sometimes", "Not really"]},
        {"id": 15, "text": "Are you good at explaining things to others?", "options": ["Yes, a lot", "Sometimes", "Not really"]},
        {"id": 16, "text": "Do you enjoy acting or performing on stage?", "options": ["Yes, a lot", "Sometimes", "Not really"]},
        {"id": 17, "text": "Do you like collecting things like stamps, coins, or rocks?", "options": ["Yes, a lot", "Sometimes", "Not really"]},
        {"id": 18, "text": "Do you enjoy cooking or baking with family?", "options": ["Yes, a lot", "Sometimes", "Not really"]},
        {"id": 19, "text": "Do you like learning new languages?", "options": ["Yes, a lot", "Sometimes", "Not really"]},
        {"id": 20, "text": "Do you enjoy solving riddles or brain teasers?", "options": ["Yes, a lot", "Sometimes", "Not really"]},
        {"id": 21, "text": "Do you like building things with LEGO or blocks?", "options": ["Yes, a lot", "Sometimes", "Not really"]},
        {"id": 22, "text": "Do you enjoy planning trips or schedules?", "options": ["Yes, a lot", "Sometimes", "Not really"]},
        {"id": 23, "text": "Are you interested in how plants grow or animals behave?", "options": ["Yes, a lot", "Sometimes", "Not really"]},
        {"id": 24, "text": "Do you like video editing or making digital art?", "options": ["Yes, a lot", "Sometimes", "Not really"]},
        {"id": 25, "text": "Do you enjoy learning about history or ancient civilizations?", "options": ["Yes, a lot", "Sometimes", "Not really"]}
    ]
    
    college_questions = [
        {"id": 1, "text": "Do you prefer theoretical research or hands-on projects?", "options": ["Theoretical Research", "Both equally", "Hands-on Projects"]},
        {"id": 2, "text": "Do you enjoy data analysis and statistics?", "options": ["Yes, a lot", "Sometimes", "Not really"]},
        {"id": 3, "text": "Are you interested in entrepreneurship and startups?", "options": ["Very interested", "Somewhat", "Not at all"]},
        {"id": 4, "text": "Do you like teaching or mentoring juniors?", "options": ["Yes, a lot", "Sometimes", "Not really"]},
        {"id": 5, "text": "Do you enjoy coding or developing software?", "options": ["Yes, a lot", "Sometimes", "Not really"]},
        {"id": 6, "text": "Are you interested in financial markets or investing?", "options": ["Very interested", "Somewhat", "Not at all"]},
        {"id": 7, "text": "Do you like writing essays, blogs, or research papers?", "options": ["Yes, a lot", "Sometimes", "Not really"]},
        {"id": 8, "text": "Do you enjoy public speaking or presentations?", "options": ["Yes, a lot", "Sometimes", "Not really"]},
        {"id": 9, "text": "Are you interested in psychology or human behavior?", "options": ["Very interested", "Somewhat", "Not at all"]},
        {"id": 10, "text": "Do you like working with robots or electronics?", "options": ["Yes, a lot", "Sometimes", "Not really"]},
        {"id": 11, "text": "Do you enjoy social media management or digital marketing?", "options": ["Yes, a lot", "Sometimes", "Not really"]},
        {"id": 12, "text": "Are you interested in environmental sustainability?", "options": ["Very interested", "Somewhat", "Not at all"]},
        {"id": 13, "text": "Do you like designing graphics or user interfaces?", "options": ["Yes, a lot", "Sometimes", "Not really"]},
        {"id": 14, "text": "Do you enjoy scientific lab work or experiments?", "options": ["Yes, a lot", "Sometimes", "Not really"]},
        {"id": 15, "text": "Are you interested in law, politics, or governance?", "options": ["Very interested", "Somewhat", "Not at all"]},
        {"id": 16, "text": "Do you like event management or coordinating teams?", "options": ["Yes, a lot", "Sometimes", "Not really"]},
        {"id": 17, "text": "Do you enjoy traveling and learning new cultures?", "options": ["Yes, a lot", "Sometimes", "Not really"]},
        {"id": 18, "text": "Are you good at negotiating or persuading people?", "options": ["Yes, a lot", "Sometimes", "Not really"]},
        {"id": 19, "text": "Do you like photography or filmmaking?", "options": ["Yes, a lot", "Sometimes", "Not really"]},
        {"id": 20, "text": "Are you interested in AI and machine learning?", "options": ["Very interested", "Somewhat", "Not at all"]},
        {"id": 21, "text": "Do you enjoy volunteering or social work?", "options": ["Yes, a lot", "Sometimes", "Not really"]},
        {"id": 22, "text": "Do you like solving complex real-world problems?", "options": ["Yes, a lot", "Sometimes", "Not really"]},
        {"id": 23, "text": "Are you interested in animation or game design?", "options": ["Very interested", "Somewhat", "Not at all"]},
        {"id": 24, "text": "Do you enjoy writing business plans or case studies?", "options": ["Yes, a lot", "Sometimes", "Not really"]},
        {"id": 25, "text": "Do you like creating YouTube videos or podcasts?", "options": ["Yes, a lot", "Sometimes", "Not really"]}
    ]
    
    if user_type == 'school':
        return school_questions
    else:
        return college_questions

# ==================== PERSONALITY PATHWAY ANALYSIS ====================
def analyze_personality_pathway(responses, user_type):
    """Analyze personality responses and return pathway recommendation"""
    # Calculate scores based on responses
    visual_score = 0
    auditory_score = 0
    kinesthetic_score = 0
    reading_score = 0
    
    # Sample mapping - you can customize this based on your questions
    for q_id, answer in responses.items():
        if answer in ["Yes, a lot", "Very interested", "Always"]:
            visual_score += 3
            kinesthetic_score += 2
        elif answer in ["Sometimes", "Somewhat"]:
            visual_score += 2
            auditory_score += 2
            reading_score += 2
        else:
            reading_score += 3
            auditory_score += 2
    
    scores = {
        "Visual Learner": visual_score,
        "Auditory Learner": auditory_score,
        "Kinesthetic Learner": kinesthetic_score,
        "Reading/Writing Learner": reading_score
    }
    
    dominant = max(scores, key=scores.get)
    percentage = int((scores[dominant] / sum(scores.values())) * 100)
    
    pathways = {
        "Visual Learner": {
            "icon": "👁️🎨",
            "title": "Visual Learner",
            "description": "You learn best through visual aids like diagrams, charts, videos, and written instructions. You remember information better when it's presented visually.",
            "strengths": ["Strong visual memory", "Good at spatial relationships", "Excellent at reading maps and diagrams", "Detail-oriented"],
        },
        "Auditory Learner": {
            "icon": "🎧🗣️",
            "title": "Auditory Learner",
            "description": "You learn best through listening - lectures, discussions, audio books, and verbal explanations. You remember information through sound and rhythm.",
            "strengths": ["Excellent listening skills", "Good at verbal instructions", "Strong memory for spoken information", "Great at public speaking"],
        },
        "Kinesthetic Learner": {
            "icon": "✋🏃",
            "title": "Kinesthetic Learner",
            "description": "You learn best through hands-on activities, movement, and physical experiences. You remember information by doing and practicing.",
            "strengths": ["Excellent hand-eye coordination", "Good at physical activities", "Strong problem-solving through action", "Practical and hands-on"],
        },
        "Reading/Writing Learner": {
            "icon": "📚✍️",
            "title": "Reading/Writing Learner",
            "description": "You learn best through reading and writing - books, articles, notes, and essays. You excel at expressing ideas through text.",
            "strengths": ["Strong reading comprehension", "Excellent writing skills", "Good at research", "Detail-oriented in documentation"],
        }
    }
    
    pathway = pathways.get(dominant, pathways["Visual Learner"])
    pathway["match_percentage"] = percentage
    pathway["personality_type"] = dominant
    
    return pathway

def calculate_results(responses, questions_list, categories):
    """Calculate weighted scores for each category"""
    # Reset scores
    for cat_id in categories:
        categories[cat_id]['score'] = 0
        categories[cat_id]['question_count'] = 0
    
    # Calculate weighted scores
    for q_id, score in responses.items():
        for q in questions_list:
            if q['id'] == q_id:
                cat_id = q['category_id']
                weight = q.get('weight', 1.0)
                weighted_score = int(score) * weight
                categories[cat_id]['score'] += weighted_score
                categories[cat_id]['question_count'] += 1
                break
    
    # Calculate percentages
    for cat_id in categories:
        cat = categories[cat_id]
        if cat['question_count'] > 0:
            # Each question max is 5, multiplied by weights
            # Calculate actual max possible based on weights
            total_weight = 0
            for q in questions_list:
                if q['category_id'] == cat_id:
                    total_weight += q.get('weight', 1.0)
            max_score = total_weight * 5
            
            if max_score > 0:
                cat['score'] = (cat['score'] / max_score) * 100
            else:
                cat['score'] = 0
        else:
            cat['score'] = 0
    
    # Sort categories by score and return top 3
    sorted_cats = sorted(categories.items(), key=lambda x: x[1]['score'], reverse=True)
    recommended = [cat[0] for cat in sorted_cats[:3]]
    
    return categories, recommended

def generate_pdf_report():
    """Generate PDF report for download with proper Unicode support"""
    
    from fpdf import FPDF
    import tempfile
    import os
    import re
    
    stream = st.session_state.get('selected_stream_data') or {}
    stream_name = stream.get('stream_name', 'Selected Stream')
    score_value = stream.get('match_percentage', 0)

    recommendation = {
        "status": "success",
        "message": stream.get('explanation', 'AI recommendations have not been generated yet.'),
    }

    # Custom PDF class with Unicode support
    class PDF(FPDF):
        def header(self):
            # Add logo or header if needed
            pass
        
        def footer(self):
            self.set_y(-15)
            self.set_font('helvetica', 'I', 8)
            self.set_text_color(128, 128, 128)
            self.cell(0, 10, f'Page {self.page_no()}', 0, 0, 'C')
    
    # Create PDF
    pdf = PDF()
    pdf.add_page()
    
    # Try to add Unicode font, fallback to helvetica
    try:
        pdf.add_font('helvetica', '', 'helvetica.ttf', uni=True)
        pdf.set_font('helvetica', '', 12)
    except:
        pdf.set_font('helvetica', '', 12)
    
    # Helper function to clean text (remove emojis and special chars)
    def clean_text(text):
        # Remove emojis and special characters that cause issues
        emoji_pattern = re.compile("["
            u"\U0001F600-\U0001F64F"  # emoticons
            u"\U0001F300-\U0001F5FF"  # symbols & pictographs
            u"\U0001F680-\U0001F6FF"  # transport & map symbols
            u"\U0001F1E0-\U0001F1FF"  # flags (iOS)
            u"\U00002702-\U000027B0"
            u"\U000024C2-\U0001F251"
            u"\U0001F900-\U0001F9FF"  # supplemental symbols
            u"\U0001FA70-\U0001FAFF"  # more emojis
            "]+", flags=re.UNICODE)
        text = emoji_pattern.sub(r'', text)
        # Replace bullet points with asterisks
        text = text.replace('•', '-').replace('●', '-').replace('○', '-')
        # Remove other special characters
        text = text.encode('ascii', 'ignore').decode('ascii')
        return text.strip()
    
    # Helper function to add multi-cell text safely
    def safe_multi_cell(pdf, width, height, text, border=0, align='L'):
        clean = clean_text(text)
        pdf.multi_cell(width, height, clean, border, align)
    
    # Helper function to add cell text safely
    def safe_cell(pdf, width, height, text, border=0, ln=0, align='L'):
        clean = clean_text(text)
        pdf.cell(width, height, clean, border, ln, align)
    
    # Title
    pdf.set_font('helvetica', 'B', 20)
    pdf.set_text_color(211, 84, 0)  # Orange
    safe_cell(pdf, 0, 10, "CoActions Career Counselling Report", ln=True, align='C')
    pdf.ln(10)
    
    # Student Information
    pdf.set_font('helvetica', 'B', 14)
    pdf.set_text_color(46, 125, 50)  # Green
    pdf.cell(0, 8, "Student Information", ln=True)
    pdf.set_font('helvetica', '', 11)
    pdf.set_text_color(0, 0, 0)
    
    safe_cell(pdf, 0, 6, f"Name: {st.session_state.student_name}", ln=True)
    safe_cell(pdf, 0, 6, f"Age: {st.session_state.student_age}", ln=True)
    safe_cell(pdf, 0, 6, f"Institution: {st.session_state.student_institution}", ln=True)
    safe_cell(pdf, 0, 6, f"City: {st.session_state.student_city}", ln=True)
    safe_cell(pdf, 0, 6, f"State: {st.session_state.student_state}", ln=True)
    safe_cell(pdf, 0, 6, f"Grade/Year: {st.session_state.student_grade}", ln=True)
    safe_cell(pdf, 0, 6, f"Assessment Type: {'School Student' if st.session_state.user_type == 'school' else 'College Student'} Pathway", ln=True)
    safe_cell(pdf, 0, 6, f"Date: {datetime.now().strftime('%Y-%m-%d')}", ln=True)
    pdf.ln(5)
    
    # Selected Stream
    pdf.set_font('helvetica', 'B', 14)
    pdf.set_text_color(211, 84, 0)
    pdf.cell(0, 8, "Selected Stream", ln=True)
    pdf.set_font('helvetica', '', 11)
    pdf.set_text_color(0, 0, 0)
    safe_cell(pdf, 0, 6, f"Stream: {stream_name}", ln=True)
    safe_cell(pdf, 0, 6, f"Match Score: {score_value:.1f}%", ln=True)
    pdf.ln(5)

    # AI Recommendation
    pdf.set_font('helvetica', 'B', 14)
    pdf.set_text_color(46, 125, 50)
    pdf.cell(0, 8, "AI Recommendation", ln=True)
    pdf.set_font('helvetica', '', 11)
    pdf.set_text_color(0, 0, 0)
    safe_multi_cell(pdf, 0, 6, recommendation.get('message', 'Recommendations are not available yet.'))
    pdf.ln(5)

    # AI Analysis - Strengths & Opportunities only (never Weaknesses/Threats)
    analysis = st.session_state.get('ai_analysis')
    if analysis:
        pdf.set_font('helvetica', 'B', 14)
        pdf.set_text_color(211, 84, 0)
        pdf.cell(0, 8, "AI Analysis", ln=True)

        pdf.set_font('helvetica', 'B', 12)
        pdf.set_text_color(46, 125, 50)
        pdf.cell(0, 7, "Strengths", ln=True)
        pdf.set_font('helvetica', '', 11)
        pdf.set_text_color(0, 0, 0)
        for s in analysis.get('strengths', []):
            safe_multi_cell(pdf, 0, 6, f"- {s}")
        pdf.ln(2)

        pdf.set_font('helvetica', 'B', 12)
        pdf.set_text_color(46, 125, 50)
        pdf.cell(0, 7, "Opportunities", ln=True)
        pdf.set_font('helvetica', '', 11)
        pdf.set_text_color(0, 0, 0)
        for o in analysis.get('opportunities', []):
            safe_multi_cell(pdf, 0, 6, f"- {o}")
        pdf.ln(5)

    # Full AI deep-dive report
    deep_dive = st.session_state.get('ai_deep_dive')
    if deep_dive:
        def pdf_section(title, body):
            pdf.set_font('helvetica', 'B', 13)
            pdf.set_text_color(211, 84, 0)
            pdf.cell(0, 8, title, ln=True)
            pdf.set_font('helvetica', '', 11)
            pdf.set_text_color(0, 0, 0)
            if isinstance(body, list):
                for item in body:
                    safe_multi_cell(pdf, 0, 6, f"- {item}")
            else:
                safe_multi_cell(pdf, 0, 6, str(body))
            pdf.ln(3)

        pdf_section("Career Overview", deep_dive.get("career_overview", ""))
        pdf_section("Future Scope", deep_dive.get("future_scope", ""))
        pdf_section("Technical Skills", deep_dive.get("technical_skills", []))
        pdf_section("Soft Skills", deep_dive.get("soft_skills", []))
        pdf_section("Major Hiring Cities in India", deep_dive.get("major_hiring_cities_india", []))
        pdf_section("Education Path", deep_dive.get("education_path", ""))
        pdf_section("Certifications", deep_dive.get("certifications", []))
        pdf_section("Learning Resources", deep_dive.get("learning_resources", []))
        pdf_section("Related Career Roles", deep_dive.get("related_career_roles", []))

    # Save to temporary file
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.pdf')
    pdf.output(temp_file.name)
    temp_file.close()
    
    return temp_file.name

# ==================== COMPLETE STREAM DETAILS DATABASE ====================
# This covers ALL possible streams from both school and college JSON files

# ==================== GEMINI API CONFIGURATION ====================

class GeminiConfigError(Exception):
    """Raised when the Gemini API key/client cannot be configured."""
    pass


class GeminiConnectionError(Exception):
    """Raised when a call to the Gemini API fails at runtime."""
    pass


def _get_gemini_api_key():
    """
    Safely fetch the Gemini API key.
    Priority: st.secrets["GEMINI_API_KEY"] -> environment variable GEMINI_API_KEY.
    Returns None if not configured anywhere. The key is never hardcoded
    and never written to logs or the UI.
    """
    try:
        if "GEMINI_API_KEY" in st.secrets:
            key = st.secrets["GEMINI_API_KEY"]
            if key:
                return key
    except Exception:
        # st.secrets raises if no secrets.toml exists at all - that's fine, fall back to env var
        pass
    return os.environ.get("GEMINI_API_KEY")


class _GeminiModel:
    """
    Thin adapter around the new `google-genai` SDK's `client.models`
    surface that preserves the `model.generate_content(prompt,
    generation_config=...)` call shape the rest of this file already
    uses. This keeps every call site below (and `safe_generate_json_content`'s
    dict-based `generation_config`) unchanged, while routing the actual
    request through `client.models.generate_content(model=..., contents=...,
    config=types.GenerateContentConfig(...))` under the hood.
    """

    def __init__(self, client: "genai.Client", model_name: str):
        self._client = client
        self._model_name = model_name

    def generate_content(self, prompt, generation_config=None):
        config = None
        if generation_config:
            cfg_kwargs = {}
            if "max_output_tokens" in generation_config:
                cfg_kwargs["max_output_tokens"] = generation_config["max_output_tokens"]
            if generation_config.get("response_mime_type"):
                cfg_kwargs["response_mime_type"] = generation_config["response_mime_type"]
            if "thinking_config" in generation_config:
                tc = generation_config["thinking_config"] or {}
                cfg_kwargs["thinking_config"] = types.ThinkingConfig(
                    thinking_budget=tc.get("thinking_budget", 0)
                )
            config = types.GenerateContentConfig(**cfg_kwargs)

        return self._client.models.generate_content(
            model=self._model_name,
            contents=prompt,
            config=config,
        )


@st.cache_resource(show_spinner=False)
def _build_gemini_client(api_key: str, model_name: str):
    """
    Actually construct the Gemini client. Cached on BOTH api_key and
    model_name so that editing secrets.toml / the GEMINI_API_KEY env var
    and re-running the app automatically picks up the new key instead of
    silently reusing a client built with the old one.
    """
    client = genai.Client(api_key=api_key)
    return _GeminiModel(client, model_name)


def get_gemini_client(model_name: str = "gemini-2.5-flash"):
    """
    Build (and transparently cache) a reusable Gemini client.

    This is the single place in the app that talks to the `google-genai`
    SDK for configuration - all future Gemini calls should reuse the
    model instance returned here instead of calling
    genai.Client()/client.models.generate_content() directly.

    The underlying client is cached via st.cache_resource, keyed on the
    actual api_key + model_name, so changing the key in secrets.toml (or
    the GEMINI_API_KEY env var) and rerunning Streamlit automatically
    builds a fresh client instead of reusing a stale one.

    Raises:
        GeminiConfigError: if no API key is configured, or the SDK
            fails to initialize for any reason (invalid key format,
            SDK/network issues at configure-time, etc).

    Returns:
        _GeminiModel: a ready-to-use Gemini client adapter wrapping
        genai.Client.
    """
    api_key = _get_gemini_api_key()
    if not api_key:
        raise GeminiConfigError(
            "Gemini API key not found. Set it via Streamlit secrets "
            "(st.secrets['GEMINI_API_KEY']) or the GEMINI_API_KEY "
            "environment variable."
        )

    try:
        return _build_gemini_client(api_key, model_name)
    except Exception as e:
        raise GeminiConfigError(f"Failed to initialize the Gemini client: {str(e)}")


def check_gemini_connection(model_name: str = "gemini-2.5-flash"):
    """
    Verify that a working Gemini API connection can be established.

    This performs a lightweight call to confirm the API key is valid and
    the service is reachable. It does NOT generate any career
    recommendations - it only validates connectivity for diagnostic /
    setup purposes.

    Returns:
        dict: {"connected": bool, "message": str}
    """
    try:
        model = get_gemini_client(model_name)
    except GeminiConfigError as e:
        return {"connected": False, "message": str(e)}

    try:
        response = model.generate_content("Reply with the single word: OK")
        reply_text = (getattr(response, "text", "") or "").strip()
        if reply_text:
            return {"connected": True, "message": "Gemini API connection successful."}
        return {
            "connected": False,
            "message": "Gemini API responded but returned no content. "
                        "The connection may be unstable.",
        }
    except Exception as e:
        return {
            "connected": False,
            "message": f"Gemini API call failed: {str(e)}",
        }


def make_response_fingerprint(*parts):
    """
    Build a stable fingerprint string from the given parts (questionnaire
    responses, personality responses, user_type, stream name, role name,
    etc). Used to decide whether a cached Gemini response can be reused or
    whether the underlying inputs changed and a fresh call is needed.
    """
    import hashlib
    normalized = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def safe_generate_json_content(model, prompt, max_output_tokens=2048):
    """
    Call Gemini with JSON response mode, falling back to a plain call if the
    `google-genai` SDK/model in use doesn't support a given config option
    (e.g. an older/different Gemini model that rejects response_mime_type
    or thinking_config).

    ROOT CAUSE OF "Unterminated string... response looks cut off mid-JSON":
    -------------------------------------------------------------------
    The model used by default is "gemini-2.5-flash", which is a *thinking*
    model. Gemini 2.5 models spend part of their max_output_tokens budget
    on an internal "thinking"/reasoning pass BEFORE emitting the visible
    answer text. That hidden thinking output is counted against the same
    max_output_tokens cap as the visible JSON answer.

    With max_output_tokens set as low as 1536-3072 (see the various call
    sites in this file), the thinking pass alone can consume most or all
    of that budget, leaving only a few hundred tokens (or fewer) for the
    actual JSON answer. The model then gets cut off mid-stream by the
    token limit, producing exactly the symptom reported: a response that
    *looks* like valid JSON at the start but ends abruptly with an
    unterminated string/object (response.candidates[0].finish_reason ==
    "MAX_TOKENS").

    This is NOT a markdown-fence problem and NOT a "Gemini returned plain
    text" problem - the existing ```json fence-stripping and json.loads()
    calls were already correct. The response text itself was genuinely
    incomplete JSON because it got truncated before parsing ever happened.

    THE FIX (two parts, both required):
    1. Explicitly disable the thinking budget via `thinking_config` so the
       full max_output_tokens budget is available for the visible JSON
       answer instead of being silently eaten by hidden reasoning tokens.
       This is tried first, with graceful fallbacks if the installed
       SDK/model doesn't support `thinking_config`.
    2. Surface the *real* reason for truncation (finish_reason) via the
       new `_log_gemini_debug_info()` helper below, called from every
       generate_ai_* function BEFORE the JSON is parsed - so any future
       failure is diagnosable from logs instead of guesswork.
    """
    base_config = {
        "max_output_tokens": max_output_tokens,
        "response_mime_type": "application/json",
    }

    # Attempt 1: JSON mode + thinking budget disabled - the real fix, since
    # it leaves the full max_output_tokens budget free for the JSON output
    # instead of letting hidden "thinking" tokens eat into it.
    try:
        return model.generate_content(
            prompt,
            generation_config={
                **base_config,
                "thinking_config": {"thinking_budget": 0},
            },
        )
    except Exception as e:
        print(f"[Gemini DEBUG] thinking_config not supported by this SDK/model "
              f"({e}); falling back to JSON mode without thinking_config.")

    # Attempt 2: JSON mode without thinking_config (older SDKs that don't
    # recognise the kwarg at all - same behaviour as before this fix).
    try:
        return model.generate_content(prompt, generation_config=base_config)
    except Exception as e:
        print(f"[Gemini DEBUG] response_mime_type=application/json not "
              f"supported ({e}); falling back to a plain text call.")

    # Attempt 3: plain call, no JSON mode (oldest SDKs). Markdown-fence
    # stripping in each parser below still handles this case.
    return model.generate_content(
        prompt,
        generation_config={"max_output_tokens": max_output_tokens},
    )


def _strip_markdown_json(response_text):
    """
    Remove markdown code-fence formatting (```json ... ``` or plain ``` ... ```)
    that Gemini sometimes wraps its JSON output in, even when JSON response
    mode is requested. Always strips surrounding whitespace first/last.

    This is intentionally a pure string transform with no parsing - it never
    raises, and it is safe to call on any text (including empty strings or
    text that isn't fenced at all, which is returned unchanged but
    whitespace-stripped).
    """
    cleaned = (response_text or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        # Drop a leading language hint like "json\n"
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    return cleaned


def _safe_json_loads(cleaned_text, label):
    """
    The ONLY place in this file allowed to call json.loads() on Gemini
    output. Never call json.loads() directly on unchecked AI text anywhere
    else - always go through this function (or generate_validated_json
    below, which uses it).

    Validates the text first (non-empty, looks like a JSON object/array),
    then attempts to parse it. Catches json.JSONDecodeError specifically
    (rather than a bare except) so we never accidentally swallow unrelated
    bugs, logs the raw text that failed to parse for debugging, and returns
    None on any failure instead of raising - so a bad AI response can never
    crash the app.

    Returns:
        dict | list | None: the parsed JSON value, or None if the text was
        empty, structurally invalid, or not valid JSON.
    """
    if not cleaned_text:
        print(f"[Gemini DEBUG] [{label}] _safe_json_loads: empty text after cleaning - nothing to parse.")
        return None

    # Cheap structural pre-check before we even attempt to parse: a valid
    # JSON object/array must start with { or [. This catches "Gemini
    # returned plain prose instead of JSON" early, with a clear log line,
    # rather than letting json.loads() raise a more confusing error.
    if cleaned_text[0] not in "{[":
        print(f"[Gemini DEBUG] [{label}] _safe_json_loads: text does not start "
              f"with '{{' or '[' - this looks like plain text, not JSON. "
              f"First 200 chars: {cleaned_text[:200]!r}")
        return None

    try:
        return json.loads(cleaned_text, strict=False)
    except json.JSONDecodeError as e:
        print(f"[Gemini DEBUG] [{label}] _safe_json_loads: JSONDecodeError: {e}")
        print(f"[Gemini DEBUG] [{label}] raw text that failed to parse:\n{cleaned_text}")
        return None


def generate_validated_json(model, prompt, max_output_tokens, label, validator=None):
    """
    The single, shared "ask Gemini for JSON and get back a trustworthy
    Python object" pipeline. Every generate_ai_* function below should call
    this instead of hand-rolling its own generate -> strip -> json.loads()
    sequence.

    Pipeline:
        1. Call Gemini with JSON response mode (response_mime_type=
           "application/json") via safe_generate_json_content - this is the
           "always request structured JSON output" requirement; it's not
           optional/best-effort, every call site goes through it.
        2. Log the raw response (finish_reason, length, truncation check)
           for debugging, BEFORE any parsing is attempted.
        3. Strip markdown code fences if present (_strip_markdown_json).
        4. Validate + parse via _safe_json_loads - this NEVER calls
           json.loads() on unchecked text; it pre-validates structure and
           catches JSONDecodeError internally.
        5. If a `validator` callable was provided, run it on the parsed
           data. The validator is responsible for checking the response
           matches the EXPECTED SCHEMA (required fields present, right
           types/shape) while ignoring any optional/extra fields, and
           must either return the cleaned/normalised result or raise
           ValueError if the schema doesn't match. A schema mismatch is
           treated exactly like a JSON parse failure below - it triggers
           a regeneration, not a partially-displayed result.
        6. If parsing OR schema validation failed, retry the ENTIRE Gemini
           call exactly once (fresh request - a different sample from the
           model often succeeds even when the first one was truncated,
           malformed, or incomplete). The retry goes through the exact
           same log -> strip -> validate -> schema-check steps.
        7. If the retry also fails, return None. Callers MUST treat None as
           "could not get a usable, schema-valid response" and show a
           friendly error instead of proceeding - they must never fall
           back to displaying the raw/partially-parsed text.

    Returns:
        tuple[Any | None, str]: (validated_result_or_None, last_raw_response_text)
        When `validator` is given and succeeds, the first element is
        whatever the validator returned (its normalised result) rather
        than the raw parsed JSON. The raw text is always returned (even on
        failure) so callers can continue to store it in session_state for
        debugging, exactly as before.
    """
    last_response_text = ""

    for attempt in (1, 2):
        attempt_label = f"{label} (attempt {attempt}/2)"
        response = safe_generate_json_content(model, prompt, max_output_tokens=max_output_tokens)
        response_text = (getattr(response, "text", "") or "").strip()
        last_response_text = response_text

        _log_gemini_debug_info(attempt_label, response, response_text)

        if not response_text:
            print(f"[Gemini DEBUG] [{attempt_label}] Gemini returned an empty response.")
            if attempt == 1:
                print(f"[Gemini DEBUG] [{label}] Retrying once with a fresh Gemini call...")
            continue

        cleaned = _strip_markdown_json(response_text)
        data = _safe_json_loads(cleaned, attempt_label)

        if data is None:
            print(f"[Gemini DEBUG] [{attempt_label}] JSON parsing failed.")
            if attempt == 1:
                print(f"[Gemini DEBUG] [{label}] Retrying once with a fresh Gemini call...")
            continue

        if validator is not None:
            try:
                validated = validator(data)
            except ValueError as schema_error:
                print(f"[Gemini DEBUG] [{attempt_label}] Schema validation failed: {schema_error}")
                if attempt == 1:
                    print(f"[Gemini DEBUG] [{label}] Response was valid JSON but did not match "
                          f"the expected schema - retrying once with a fresh Gemini call...")
                continue
            data = validated

        if attempt == 2:
            print(f"[Gemini DEBUG] [{label}] Succeeded on retry (attempt 2/2).")
        return data, response_text

    print(f"[Gemini DEBUG] [{label}] Both attempts failed to produce valid JSON. Giving up.")
    return None, last_response_text


def _log_gemini_debug_info(label, response, response_text):
    """
    Print full diagnostic info about a raw Gemini response BEFORE it is
    parsed as JSON. This is the "print the raw response" + truncation
    detection requested for debugging the parse-failure pipeline.

    Logs (to stdout / Streamlit's server console - never shown to the end
    user, so existing UI behaviour is unchanged):
      - which generation step this came from (label)
      - finish_reason for the first candidate, e.g. "STOP" (normal),
        "MAX_TOKENS" (truncated - the real bug), "SAFETY", "RECITATION".
      - the full raw text Gemini returned, exactly as received.
      - whether the text looks like markdown-fenced JSON, and whether it
        looks truncated (doesn't end in a closing brace/bracket/fence).
    """
    finish_reason = None
    try:
        candidates = getattr(response, "candidates", None) or []
        if candidates:
            raw_finish_reason = getattr(candidates[0], "finish_reason", None)
            # The new google-genai SDK returns a FinishReason enum (e.g.
            # FinishReason.STOP) rather than a plain string/int - normalize
            # to its bare name (e.g. "STOP") so the STOP/MAX_TOKENS string
            # comparisons below keep working exactly as before.
            finish_reason = getattr(raw_finish_reason, "name", raw_finish_reason)
    except Exception as e:
        finish_reason = f"<could not read finish_reason: {e}>"

    stripped = (response_text or "").strip()
    looks_fenced = stripped.startswith("```")
    looks_truncated = bool(stripped) and stripped[-1] not in "}]`"

    print(f"\n[Gemini DEBUG] ===== {label} =====")
    print(f"[Gemini DEBUG] finish_reason: {finish_reason}")
    print(f"[Gemini DEBUG] response length: {len(stripped)} chars")
    print(f"[Gemini DEBUG] markdown-fenced JSON: {looks_fenced}")
    print(f"[Gemini DEBUG] looks truncated (doesn't end in }}/]/`): {looks_truncated}")
    print(f"[Gemini DEBUG] raw response text:\n{response_text}")
    print(f"[Gemini DEBUG] ===== end {label} =====\n")

    if str(finish_reason) not in ("STOP", "1", "None"):
        print(f"[Gemini DEBUG] WARNING: finish_reason={finish_reason} - if this "
              f"is MAX_TOKENS, the response was cut off before completion. "
              f"Consider raising max_output_tokens for this call.")


# ==================== AI PROMPT BUILDER ====================

def generate_ai_prompt(student_details, questionnaire_responses, personality_responses, user_type):
    """
    Build the complete, dynamic prompt that will be sent to Gemini.

    This function ONLY assembles prompt text - it does not call the
    Gemini API itself.

    Args:
        student_details (dict): Student registration info. Expected keys
            include 'name', 'age', 'institution', 'city', 'state', 'grade'.
        questionnaire_responses (dict): Raw {question_id: rating} answers
            from the career questionnaire.
        personality_responses (dict): Raw {question_id: answer} answers
            from the optional personality assessment (may be empty).
        user_type (str): Either 'school' or 'college'.

    Returns:
        str: A complete prompt instructing Gemini to analyse the
        student's interests, aptitude, learning style, and career
        preferences, and to return ONLY the top 3 recommended career
        streams as a small, strict JSON payload (stream_name,
        match_percentage, reason per stream) - nothing else. This is the
        FIRST of several Gemini calls in the app; all other details
        (skills, salary, companies, roadmap, certifications, education
        path, future scope, resources, strengths/opportunities analysis)
        are deliberately deferred to later, separate API calls
        (generate_ai_analysis / generate_ai_deep_dive /
        generate_ai_role_detail) so this first request stays small, fast,
        and reliable.
    """
    name = student_details.get('name', 'The student')
    age = student_details.get('age', 'Not specified')
    grade = student_details.get('grade', 'Not specified')
    education_level = "School Student" if user_type == 'school' else "College Student"
    grade_label = "Grade" if user_type == 'school' else "Year"

    questionnaire_responses = questionnaire_responses or {}
    personality_responses = personality_responses or {}

    # ---- OPTIMIZATION 1: compact interest summary instead of raw Q&A ----
    # Gemini doesn't need every individual question/answer pair (often
    # ~25 separate "- Question N: answer" lines) - it only needs the
    # *aggregate signal* those answers produce: how strongly the student
    # leans toward each interest category. `calculate_results()` already
    # turns the raw answers into a 0-100 score per category and stores it
    # in st.session_state.categories_data once the questionnaire is
    # submitted, so we reuse that instead of resending raw answers. This
    # collapses ~25 lines of raw Q&A into a handful of "Category: score"
    # lines carrying the same decision-relevant information.
    categories_data = st.session_state.get('categories_data') or {}
    scored_categories = sorted(
        (
            (cat.get('name') or cat.get('category_name') or cat_id, round(cat.get('score', 0)))
            for cat_id, cat in categories_data.items()
            if cat.get('question_count', 0) > 0
        ),
        key=lambda x: x[1],
        reverse=True,
    )
    if scored_categories:
        # OPTIMIZATION 2: cap to the top categories. Only the
        # highest-scoring categories can plausibly drive a "top 3"
        # recommendation - low-scoring ones add tokens without adding
        # decision-relevant signal, so the tail is dropped.
        interest_lines = "\n".join(f"{cat_name}: {score}" for cat_name, score in scored_categories[:6])
    elif questionnaire_responses:
        # Fallback if scores aren't available yet - still avoid dumping
        # every raw answer; just note that responses exist.
        interest_lines = f"{len(questionnaire_responses)} questionnaire questions answered (scores unavailable)."
    else:
        interest_lines = "No questionnaire responses provided."

    # ---- OPTIMIZATION 3: compact personality summary instead of raw Q&A ----
    # Same idea: `analyze_personality_pathway()` already reduces the raw
    # personality answers into a dominant learning style, a match
    # percentage, and a short list of associated traits/strengths. That
    # IS the "Top Personality Traits" summary the model needs - sending
    # all raw answers as well would just duplicate information the
    # summary already encodes.
    pathway = st.session_state.get('personality_pathway')
    if pathway:
        traits = "\n".join(f"- {t}" for t in pathway.get('strengths', [])[:4])
        personality_summary = f"{pathway.get('title', 'N/A')} ({pathway.get('match_percentage', 0)}% match)"
        if traits:
            personality_summary += f"\nTop Personality Traits:\n{traits}"
    elif personality_responses:
        personality_summary = f"{len(personality_responses)} personality questions answered (summary unavailable)."
    else:
        personality_summary = "Not taken (optional)."

    # ---- OPTIMIZATION 4: one short, non-repeated depth instruction ----
    # The original spelled out similar guidance (adapt response, language
    # level, scope) across several sentences per user_type. Reduced to a
    # single line carrying only the information that actually changes
    # model behaviour.
    if user_type == 'school':
        depth_instruction = "School student: use simple language; recommend broad streams (Science/Commerce/Arts/Vocational), not job titles."
    else:
        depth_instruction = "College student: use professional language; recommend specific industry roles/specializations, not skills or job-market detail."

    # ---- OPTIMIZATION 5: removed duplicated/repeated instructions ----
    # The original stated "JSON only, no markdown/preamble" twice,
    # repeated the "don't generate skills/salary/etc." instruction across
    # two separate paragraphs, and explained internal app architecture
    # (deferred follow-up calls) that Gemini has no use for. All of that
    # is removed - the single OUTPUT FORMAT line below is now the only
    # statement of what to return.
    prompt = f"""You are an expert career counsellor AI. Recommend the TOP 3 career streams for this student, ranked strongest to weakest match, based on the summary below (generate fresh, specific recommendations - do not use a manual database).

STUDENT: {name}, age {age}, {education_level} ({grade_label} {grade})

INTEREST SCORES (0-100)
{interest_lines}

PERSONALITY/LEARNING STYLE
{personality_summary}

{depth_instruction}

Return ONLY this JSON (no markdown, no commentary), exactly 3 items, each reason <=15 words, single-line strings:
{{"recommendations": [{{"stream_name": "string", "match_percentage": 60-99, "reason": "string"}}]}}"""

    return prompt


# ==================== JSON RESPONSE PARSING HELPER ====================

def _parse_top3_json(data):
    """
    Validate that an already-parsed JSON value (dict or list, as returned
    by generate_validated_json / _safe_json_loads) matches the EXPECTED
    SCHEMA for the first/lightweight Gemini call, and normalise it into a
    list of exactly-3 recommendation dicts: {"stream_name": str,
    "match_percentage": int, "explanation": str}.

    Used as the `validator` passed into generate_validated_json, so if this
    raises ValueError, generate_validated_json treats it the same as a
    malformed/incomplete response and automatically requests ONE
    regeneration from Gemini - the caller never sees or displays a
    partially-parsed result.

    Expected schema per recommendation entry: {"stream_name": <non-empty
    string>, "match_percentage": <number>, "reason": <non-empty string>}.
    ("explanation" is also accepted as a fallback key name, for backward
    compatibility with prompt revisions.) Any OTHER fields Gemini might
    add are simply ignored - this validator only enforces the 3 fields it
    actually needs.

    NOTE: this function does NOT call json.loads() or touch raw text at
    all - by the time data reaches here it has already been safely parsed
    upstream. It only checks/normalises *shape*.

    Raises:
        ValueError: if `recommendations` is missing, not a list, empty, or
            does not contain at least 3 entries with all required fields
            present and non-empty. This is treated as an "incomplete
            response" and triggers a regeneration.
    """
    recommendations = data.get("recommendations") if isinstance(data, dict) else data

    if not isinstance(recommendations, list) or len(recommendations) == 0:
        raise ValueError("Gemini response did not contain a recommendations list.")

    parsed = []
    for item in recommendations:
        if not isinstance(item, dict):
            continue

        stream_name = str(item.get("stream_name", "")).strip()
        # "reason" is the field requested in the lightweight first prompt;
        # "explanation" is accepted as a fallback for compatibility.
        reason = str(item.get("reason") or item.get("explanation") or "").strip()
        try:
            match_percentage = int(round(float(item.get("match_percentage", 0))))
        except (TypeError, ValueError):
            match_percentage = 0
        match_percentage = max(0, min(100, match_percentage))

        # Required fields: stream_name and reason must both be present and
        # non-empty. Optional/extra fields (anything else Gemini might add)
        # are simply ignored rather than causing a failure.
        if not stream_name or not reason:
            continue

        parsed.append({
            "stream_name": stream_name,
            "match_percentage": match_percentage,
            "explanation": reason,
        })

    if len(parsed) < 3:
        raise ValueError(
            f"Incomplete recommendations response: expected 3 complete "
            f"entries (stream_name + match_percentage + reason), got "
            f"{len(parsed)} valid out of {len(recommendations)} returned."
        )

    return parsed[:3]


# ==================== AI RECOMMENDATION (GEMINI COMMUNICATION) ====================

def generate_ai_recommendation(student_details, questionnaire_responses, personality_responses, user_type):
    """
    Send the dynamically built prompt to Gemini, parse its JSON response into
    the Top 3 recommended career streams, and store the structured result in
    session state.

    This function is responsible ONLY for the Streamlit <-> Gemini
    communication: building the prompt, calling the API, parsing the
    response, and storing the result. It deliberately does NOT render any
    UI - that is handled by show_recommendation().

    Args:
        student_details (dict): Student registration info.
        questionnaire_responses (dict): Raw career questionnaire responses.
        personality_responses (dict): Raw personality assessment responses.
        user_type (str): Either 'school' or 'college'.

    Returns:
        dict: A status payload, e.g.
            {"status": "success", "message": "..."} or
            {"status": "error", "message": "..."}
        The actual AI-generated Top 3 recommendations (if any) are stored
        separately in st.session_state.ai_top_streams as a list of
        {"stream_name", "match_percentage", "explanation"} dicts - this is
        the ONLY source of recommendation data; no manual/static data is
        ever used.
    """
    prompt = generate_ai_prompt(student_details, questionnaire_responses, personality_responses, user_type)

    try:
        model = get_gemini_client()
    except GeminiConfigError as e:
        st.session_state.gemini_response_raw = None
        st.session_state.gemini_response_error = str(e)
        st.session_state.ai_top_streams = None
        return {
            "status": "error",
            "message": "AI recommendations are temporarily unavailable "
                        "(Gemini API is not configured). Please try again later.",
        }

    try:
        # `validator=_parse_top3_json` makes schema validation part of the
        # retry loop itself: if Gemini returns syntactically valid JSON
        # that is nonetheless missing recommendations / required fields
        # (an "incomplete response"), generate_validated_json treats that
        # exactly like a parse failure and automatically regenerates once
        # before giving up - so st.session_state.ai_top_streams is only
        # ever assigned a fully-validated list of exactly 3 entries, never
        # a partially-parsed or incomplete result.
        #
        # max_output_tokens is intentionally small (512) because this
        # first-pass prompt now asks for ONLY 3 short fields per stream
        # (stream_name, match_percentage, a one-line reason) - no skills,
        # salary, companies, roadmap, certifications, education path,
        # future scope, resources, or analysis. Keeping the budget tight
        # keeps this first request fast and leaves no room for the model
        # to drift into the longer sections that are deferred to later
        # calls (generate_ai_analysis / generate_ai_deep_dive /
        # generate_ai_role_detail).
        data, response_text = generate_validated_json(
            model, prompt, max_output_tokens=512,
            label="generate_ai_recommendation",
            validator=_parse_top3_json,
        )

        # Always store the raw response for debugging - not displayed in
        # the UI - regardless of whether parsing ultimately succeeded.
        st.session_state.gemini_response_raw = response_text or None

        if data is None:
            truncated_hint = (
                " (response looks cut off mid-JSON - likely hit the output "
                "token limit; try increasing max_output_tokens)"
                if response_text and not response_text.rstrip().endswith("}")
                else ""
            )
            st.session_state.ai_top_streams = None
            st.session_state.gemini_response_error = (
                "Could not get valid AI recommendations: Gemini's response "
                f"was empty, invalid JSON, or incomplete (missing the "
                f"recommendations list or required fields), even after a "
                f"retry{truncated_hint}."
            )
            return {
                "status": "error",
                "message": "AI recommendations could not be generated right now. "
                            "Please try again later.",
            }

        # `data` here is ALREADY the fully-validated, normalised list of
        # exactly 3 {"stream_name", "match_percentage", "explanation"}
        # dicts produced by _parse_top3_json inside generate_validated_json
        # - it is safe to store and display directly.
        st.session_state.ai_top_streams = data
        st.session_state.gemini_response_error = None
        return {
            "status": "success",
            "message": "Your personalized AI recommendations have been generated.",
        }
    except Exception as e:
        st.session_state.gemini_response_raw = None
        st.session_state.gemini_response_error = str(e)
        st.session_state.ai_top_streams = None
        return {
            "status": "error",
            "message": "Something went wrong while contacting the AI recommendation "
                        "service. Please try again later.",
        }


# ==================== AI ANALYSIS (STRENGTHS + OPPORTUNITIES ONLY) ====================

def generate_ai_analysis_prompt(student_details, questionnaire_responses, stream, user_type):
    """
    Build the prompt for the AI Analysis page. Gemini must return ONLY
    Strengths and Opportunities (never Weaknesses or Threats), personalized
    to the student's questionnaire responses and the stream they selected.
    """
    name = student_details.get('name', 'The student')
    education_level = "School Student" if user_type == 'school' else "College Student"

    questionnaire_responses = questionnaire_responses or {}
    questionnaire_lines = "\n".join(
        f"- Question {q_id}: {answer}" for q_id, answer in questionnaire_responses.items()
    ) or "No questionnaire responses provided."

    if user_type == 'school':
        depth_instruction = (
            "This is a SCHOOL STUDENT: use SIMPLE, jargon-free language with short "
            "sentences and everyday vocabulary; explain every concept simply; keep "
            "strengths/opportunities framed around beginner-friendly stream selection "
            "rather than specific job roles."
        )
    else:
        depth_instruction = (
            "This is a COLLEGE STUDENT: use PROFESSIONAL, industry-appropriate "
            "language; where relevant, reference specific industry roles, "
            "specializations, skills, tools, or market context for advanced career "
            "guidance."
        )

    prompt = f"""You are an expert career counsellor AI. The student below selected the
career stream "{stream}" from your earlier recommendations.

STUDENT INFORMATION
- Name: {name}
- Education Level: {education_level}

CAREER QUESTIONNAIRE RESPONSES
{questionnaire_lines}

TASK
Generate a personalized AI Analysis for this student about the "{stream}" stream,
based ONLY on their questionnaire responses above. The analysis must include
EXACTLY two sections:
1. Strengths - personal strengths/traits this student shows (from their
   responses) that support success in this stream.
2. Opportunities - opportunities this stream and the student's profile open up
   for them (growth areas, future possibilities, advantages).

Do NOT include Weaknesses or Threats in any form - they must be completely
omitted. {depth_instruction}

Do NOT use any pre-existing/manual data - generate everything freshly for
this student.

OUTPUT FORMAT - respond with ONLY valid JSON, no markdown fences, no preamble:

{{
  "strengths": ["string", "string", "string"],
  "opportunities": ["string", "string", "string"]
}}

Provide 3 to 5 concise bullet-point style strings in each array. Every string value must be a single line with no literal line breaks (use spaces instead). Output JSON only."""

    return prompt


def _validate_analysis_schema(data):
    """
    Schema validator for the AI Analysis response, passed into
    generate_validated_json so a structurally-valid-but-incomplete response
    (e.g. missing or empty strengths/opportunities) is treated the same as
    a JSON parse failure and triggers the SAME single automatic retry,
    instead of being silently accepted or only caught after the fact.

    Raises ValueError (caught by generate_validated_json) if the schema
    doesn't match; otherwise returns the normalised {"strengths": [...],
    "opportunities": [...]} dict.
    """
    strengths = data.get("strengths", []) if isinstance(data, dict) else []
    opportunities = data.get("opportunities", []) if isinstance(data, dict) else []

    if not isinstance(strengths, list) or not isinstance(opportunities, list) \
            or len(strengths) == 0 or len(opportunities) == 0:
        raise ValueError("AI Analysis response missing strengths/opportunities.")

    return {
        "strengths": [str(s).strip() for s in strengths if str(s).strip()],
        "opportunities": [str(o).strip() for o in opportunities if str(o).strip()],
    }


def generate_ai_analysis(student_details, questionnaire_responses, stream, user_type):
    """
    Call Gemini to produce the Strengths/Opportunities AI Analysis and store
    the parsed result in st.session_state.ai_analysis.

    RELIABILITY: uses generate_validated_json, which already (a) retries
    the Gemini call exactly ONCE if JSON parsing OR schema validation
    fails - never more, so there is no risk of an infinite retry loop -
    and (b) never calls json.loads() on unchecked text. The technical
    failure detail is stored separately in st.session_state.ai_analysis_error
    for an expandable debug section; the message returned to the UI is
    always a short, friendly, non-technical sentence.
    """
    prompt = generate_ai_analysis_prompt(student_details, questionnaire_responses, stream, user_type)

    try:
        model = get_gemini_client()
    except GeminiConfigError as e:
        st.session_state.ai_analysis = None
        st.session_state.ai_analysis_error = str(e)
        return {"status": "error", "message": "AI Analysis is temporarily unavailable. Please try again later."}

    try:
        data, response_text = generate_validated_json(
            model, prompt, max_output_tokens=1536,
            label="generate_ai_analysis",
            validator=_validate_analysis_schema,
        )

        if data is None:
            st.session_state.ai_analysis = None
            st.session_state.ai_analysis_error = (
                "Gemini's response was empty, invalid JSON, or missing "
                "strengths/opportunities, even after one automatic retry."
            )
            return {
                "status": "error",
                "message": "AI Analysis could not be generated right now. Please try again later.",
            }

        st.session_state.ai_analysis = data
        st.session_state.ai_analysis_error = None
        return {"status": "success", "message": "AI Analysis generated."}
    except Exception as e:
        st.session_state.ai_analysis = None
        st.session_state.ai_analysis_error = str(e)
        return {
            "status": "error",
            "message": "Something went wrong while generating your AI Analysis. Please try again later.",
        }


# ==================== DEEP-DIVE CAREER REPORT (FULL AI REPORT) ====================

def generate_ai_deep_dive_prompt(student_details, questionnaire_responses, stream, user_type):
    """
    Build the prompt for the full post-analysis career report. Entirely
    AI-generated - no manual/static data of any kind.
    """
    name = student_details.get('name', 'The student')
    education_level = "School Student" if user_type == 'school' else "College Student"

    questionnaire_responses = questionnaire_responses or {}
    questionnaire_lines = "\n".join(
        f"- Question {q_id}: {answer}" for q_id, answer in questionnaire_responses.items()
    ) or "No questionnaire responses provided."

    if user_type == 'school':
        depth_instruction = (
            "This is a SCHOOL STUDENT: use SIMPLE language throughout; keep all "
            "sections BEGINNER-FRIENDLY and focused on foundational next steps "
            "(subjects to study, basic awareness of the field, stream selection) "
            "rather than deep professional detail; explain every concept simply."
        )
    else:
        depth_instruction = (
            "This is a COLLEGE STUDENT: use PROFESSIONAL, industry-appropriate "
            "language; keep all sections at an INDUSTRY/PROFESSIONAL level of "
            "depth, name specific industry roles and SPECIALIZATIONS, and give "
            "ADVANCED CAREER GUIDANCE referencing real-world skills, tools, "
            "certifications, and the current job market."
        )

    prompt = f"""You are an expert career counsellor AI generating a complete career report
for the stream "{stream}" for the student below.

STUDENT INFORMATION
- Name: {name}
- Education Level: {education_level}

CAREER QUESTIONNAIRE RESPONSES
{questionnaire_lines}

{depth_instruction}

Do NOT use any pre-existing/manual database - generate every field freshly
and specifically for this student and stream. Do NOT include a "Software
Skills" section under any name.

Generate EXACTLY these sections, each personalized to the student:
1. career_overview - a short paragraph describing what this career stream involves.
2. related_career_roles - a list of 5 to 8 specific job-title roles within this stream that this student could pursue (e.g. specific roles, not subfields).
3. future_scope - a short paragraph on growth/demand outlook for this stream.
4. technical_skills - a list of specific technical skills relevant to this stream.
5. soft_skills - a list of soft skills relevant to this stream.
6. major_hiring_cities_india - a list of major Indian cities where this stream has strong hiring demand.
7. education_path - a short paragraph or list describing the typical education path into this stream in India.
8. certifications - a list of relevant certifications/courses that strengthen this stream.
9. learning_resources - a list of types of learning resources (e.g. courses, books, platforms) useful for this stream.

OUTPUT FORMAT - respond with ONLY valid JSON, no markdown fences, no preamble:

{{
  "career_overview": "string",
  "related_career_roles": ["string", "..."],
  "future_scope": "string",
  "technical_skills": ["string", "..."],
  "soft_skills": ["string", "..."],
  "major_hiring_cities_india": ["string", "..."],
  "education_path": "string",
  "certifications": ["string", "..."],
  "learning_resources": ["string", "..."]
}}

Every string value must be a single line with no literal line breaks (use
spaces instead). Output JSON only."""

    return prompt


def _validate_deep_dive_schema(data):
    """
    Schema validator for the deep-dive report, passed into
    generate_validated_json so a response missing required sections
    triggers the same bounded single automatic retry as a JSON parse
    failure, rather than only being caught after the fact with no retry.
    """
    required_keys = [
        "career_overview", "related_career_roles", "future_scope", "technical_skills",
        "soft_skills", "major_hiring_cities_india", "education_path", "certifications",
        "learning_resources",
    ]
    if not isinstance(data, dict) or not all(k in data for k in required_keys):
        raise ValueError("Deep-dive response missing one or more required sections.")

    # Explicitly drop any "software_skills" field if the model adds it anyway.
    data.pop("software_skills", None)
    return data


def generate_ai_deep_dive(student_details, questionnaire_responses, stream, user_type):
    """
    Call Gemini to produce the full deep-dive career report and store the
    parsed result in st.session_state.ai_deep_dive.

    RELIABILITY: uses generate_validated_json, which retries the Gemini
    call exactly ONCE if JSON parsing OR schema validation fails (capped -
    no infinite retry loop) and never calls json.loads() on unchecked
    text. The technical failure detail is stored separately in
    st.session_state.ai_deep_dive_error for an expandable debug section;
    the message returned to the UI is always a short, friendly sentence.

    NOTE: this function definition was previously missing entirely (the
    docstring/body below were accidentally left nested inside
    generate_ai_deep_dive_prompt, which never called `return prompt` and
    therefore generate_ai_deep_dive(...) didn't exist as a callable - it
    would have raised NameError the first time show_report() tried to call
    it). Fixed as part of restructuring the JSON response handling.
    """
    prompt = generate_ai_deep_dive_prompt(student_details, questionnaire_responses, stream, user_type)

    try:
        model = get_gemini_client()
    except GeminiConfigError as e:
        st.session_state.ai_deep_dive = None
        st.session_state.ai_deep_dive_error = str(e)
        return {"status": "error", "message": "The AI career report is temporarily unavailable. Please try again later."}

    try:
        data, response_text = generate_validated_json(
            model, prompt, max_output_tokens=3072,
            label="generate_ai_deep_dive",
            validator=_validate_deep_dive_schema,
        )

        if data is None:
            st.session_state.ai_deep_dive = None
            st.session_state.ai_deep_dive_error = (
                "Gemini's response was empty, invalid JSON, or missing "
                "one or more required sections, even after one automatic retry."
            )
            return {
                "status": "error",
                "message": "The full AI report could not be generated right now. Please try again later.",
            }

        st.session_state.ai_deep_dive = data
        st.session_state.ai_deep_dive_error = None
        return {"status": "success", "message": "Deep-dive report generated."}
    except Exception as e:
        st.session_state.ai_deep_dive = None
        st.session_state.ai_deep_dive_error = str(e)
        return {
            "status": "error",
            "message": "Something went wrong while generating your AI report. Please try again later.",
        }


# ==================== CAREER ROLE DETAIL (FULLY DYNAMIC) ====================

def generate_ai_role_detail_prompt(student_details, role_name, stream, user_type):
    """
    Build the prompt for a deep-dive on a single career role the student
    clicked on. Entirely AI-generated - no hardcoded role data.
    """
    name = student_details.get('name', 'The student')
    education_level = "School Student" if user_type == 'school' else "College Student"

    if user_type == 'school':
        depth_instruction = (
            "This is a SCHOOL STUDENT: use SIMPLE, beginner-friendly language; "
            "explain concepts simply; frame this role as an early, beginner-level "
            "introduction rather than deep professional detail."
        )
    else:
        depth_instruction = (
            "This is a COLLEGE STUDENT: use PROFESSIONAL, industry-appropriate "
            "language; mention relevant specializations within this role and give "
            "ADVANCED CAREER GUIDANCE at an industry/professional level of depth."
        )

    prompt = f"""You are an expert career counsellor AI. The student below clicked on the
specific career role "{role_name}" (within the broader stream "{stream}").

STUDENT INFORMATION
- Name: {name}
- Education Level: {education_level}

{depth_instruction}

Do NOT use any pre-existing/manual role database - generate every field
freshly and specifically for the role "{role_name}" in the Indian job market.

Generate EXACTLY these sections for this role:
1. career_description - a paragraph describing what someone in this role actually does.
2. salary_range_india - the typical salary range in India (entry to experienced) as a string, in INR (e.g. lakhs per annum).
3. educational_requirements - a list of typical educational qualifications needed for this role.
4. job_responsibilities - a list of typical day-to-day responsibilities.
5. required_skills - a list of skills (technical and soft) required for this role.
6. future_job_growth - a short paragraph on the growth outlook/demand trend for this role.
7. industry_outlook - a short paragraph on the broader industry context this role sits in.
8. top_hiring_companies - a list of types/examples of companies in India that hire for this role.

OUTPUT FORMAT - respond with ONLY valid JSON, no markdown fences, no preamble:

{{
  "career_description": "string",
  "salary_range_india": "string",
  "educational_requirements": ["string", "..."],
  "job_responsibilities": ["string", "..."],
  "required_skills": ["string", "..."],
  "future_job_growth": "string",
  "industry_outlook": "string",
  "top_hiring_companies": ["string", "..."]
}}

Every string value must be a single line with no literal line breaks (use spaces instead). Output JSON only."""

    return prompt


def _validate_role_detail_schema(data):
    """
    Schema validator for the career-role detail response, passed into
    generate_validated_json so a response missing required sections
    triggers the same bounded single automatic retry as a JSON parse
    failure.
    """
    required_keys = [
        "career_description", "salary_range_india", "educational_requirements",
        "job_responsibilities", "required_skills", "future_job_growth",
        "industry_outlook", "top_hiring_companies",
    ]
    if not isinstance(data, dict) or not all(k in data for k in required_keys):
        raise ValueError("Role detail response missing one or more required sections.")
    return data


def generate_ai_role_detail(student_details, role_name, stream, user_type):
    """
    Call Gemini to produce the dynamic career role detail and store the
    parsed result in st.session_state.ai_role_detail.

    RELIABILITY: uses generate_validated_json, which retries the Gemini
    call exactly ONCE if JSON parsing OR schema validation fails (capped -
    no infinite retry loop) and never calls json.loads() on unchecked
    text. The technical failure detail is stored separately in
    st.session_state.ai_role_detail_error for an expandable debug section;
    the message returned to the UI is always a short, friendly sentence.
    """
    prompt = generate_ai_role_detail_prompt(student_details, role_name, stream, user_type)

    try:
        model = get_gemini_client()
    except GeminiConfigError as e:
        st.session_state.ai_role_detail = None
        st.session_state.ai_role_detail_error = str(e)
        return {"status": "error", "message": "This role breakdown is temporarily unavailable. Please try again later."}

    try:
        data, response_text = generate_validated_json(
            model, prompt, max_output_tokens=2048,
            label="generate_ai_role_detail",
            validator=_validate_role_detail_schema,
        )

        if data is None:
            st.session_state.ai_role_detail = None
            st.session_state.ai_role_detail_error = (
                "Gemini's response was empty, invalid JSON, or missing "
                "one or more required sections, even after one automatic retry."
            )
            return {
                "status": "error",
                "message": "This role breakdown could not be generated right now. Please try again later.",
            }

        st.session_state.ai_role_detail = data
        st.session_state.ai_role_detail_error = None
        return {"status": "success", "message": "Role detail generated."}
    except Exception as e:
        st.session_state.ai_role_detail = None
        st.session_state.ai_role_detail_error = str(e)
        return {
            "status": "error",
            "message": "Something went wrong while generating this role breakdown. Please try again later.",
        }


def get_user_type_from_grade(grade):
    """Determine user type based on grade/year"""
    school_grades = ["9th", "10th", "11th", "12th"]
    college_grades = ["1st Year College", "2nd Year College", "3rd Year College", "4th Year College"]
    
    if grade in school_grades:
        return "school"
    elif grade in college_grades:
        return "college"
    else:
        return "school"

# Header with menu
def show_header():
    col1, col2 = st.columns([2, 1])
    with col1:
        st.markdown('<div class="app-title">CoActions</div>', unsafe_allow_html=True)
    with col2:
        menu_col1, menu_col2, menu_col3 = st.columns(3)
        with menu_col1:
            if st.button("🏠 Home", key="menu_home", use_container_width=True):
                # Reset all session state for home
                for key in list(st.session_state.keys()):
                    if key not in ['page', 'show_about', 'show_contact']:
                        del st.session_state[key]
                st.session_state.page = 'welcome'
                st.session_state.show_about = False
                st.session_state.show_contact = False
                st.rerun()
        with menu_col2:
            if st.button("📖 About", key="menu_about", use_container_width=True):
                st.session_state.show_about = True
                st.session_state.show_contact = False
        with menu_col3:
            if st.button("📞 Contact", key="menu_contact", use_container_width=True):
                st.session_state.show_contact = True
                st.session_state.show_about = False

def show_welcome():
    show_header()
    
    if st.session_state.show_about:
        st.markdown('<div class="main-card">', unsafe_allow_html=True)
        st.markdown('<h1 class="welcome-heading">📖 About CoActions</h1>', unsafe_allow_html=True)
        st.markdown("""
        <div style="background:#FFF8F0; border-radius:20px; padding:1.5rem;">
            <p>🌟 <strong>CoActions</strong> is a professional career guidance platform.</p>
            <p>🎯 We help students discover their ideal career path through personalized assessments.</p>
            <p>📊 Analyzing responses across 18+ career categories to provide accurate recommendations.</p>
            <p>💡 Our AI-powered tool helps identify your strengths, interests, and potential career paths.</p>
            <p>🏆 Trusted by thousands of students to make informed career decisions.</p>
        </div>
        """, unsafe_allow_html=True)
        if st.button("← Back to Home", key="back_to_home_about"):
            st.session_state.show_about = False
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)
        return
    
    if st.session_state.show_contact:
        st.markdown('<div class="main-card">', unsafe_allow_html=True)
        st.markdown('<h1 class="welcome-heading">📞 Contact Us</h1>', unsafe_allow_html=True)
        st.markdown("""
        <div style="background:#FFF8F0; border-radius:20px; padding:1.5rem;">
            <p>📧 <strong>Email:</strong> elevatea0200@gmail.com</p>
            <p>🌐 <strong>Website:</strong> www.coactions.com</p>
            <p>💬 <strong>Support:</strong> Mon-Fri, 9AM-6PM</p>
            <p>📱 <strong>Phone:</strong> +91 12345 67890</p>
            <p>📍 <strong>Address:</strong> Career Guidance Center, Main Street, City</p>
        </div>
        """, unsafe_allow_html=True)
        if st.button("← Back to Home", key="back_to_home_contact"):
            st.session_state.show_contact = False
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)
        return
    
    st.markdown('<div class="main-card">', unsafe_allow_html=True)
    st.markdown('<div class="deco-icon">🎓✨🚀</div>', unsafe_allow_html=True)
    st.markdown('<h1 class="welcome-heading">Welcome to the Career Counselling Tool</h1>', unsafe_allow_html=True)
    st.markdown('<p class="welcome-subheading">Discover Your Perfect Career Path with Personalized Guidance</p>', unsafe_allow_html=True)
    st.markdown('<hr>', unsafe_allow_html=True)
    
    st.markdown('<h3 class="section-title">📝 Student Information</h3>', unsafe_allow_html=True)

    st.markdown("""
        <style>    
          /* For all select/dropdown elements */
          select, .stSelectbox select, [data-baseweb="select"] {
             color: black !important;
             background-color: white !important;
            }

          /* For all option items */
          option, [role="option"] {
             color: black !important;
             background-color: white !important;
            }

          /* For Streamlit selectbox specifically */
          .stSelectbox > div > div {
              color: black !important;
              background-color: white !important;
            }

          .stSelectbox [data-baseweb="select"] span {
             color: black !important;
            }
        </style>
        """, unsafe_allow_html=True)

    col1, col2 = st.columns(2)
    with col1:
        student_name = st.text_input("Full Name *", placeholder="Enter your full name", key="welcome_name")
        student_age = st.number_input("Age", min_value=10, max_value=100, step=1, key="welcome_age")
        student_city = st.text_input("City *", placeholder="Enter your city", key="welcome_city")
    with col2:
        student_institution = st.text_input("School/College/Institute", placeholder="Enter your institution", key="welcome_institution")
        student_grade = st.selectbox("Current Grade/Year *", 
                                      ["Select Grade", "9th", "10th", "11th", "12th", "1st Year College", "2nd Year College", "3rd Year College", "4th Year College"],
                                      key="welcome_grade")
        states_list = [
                  "Select State",
                  "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh", "Goa", "Gujarat", "Haryana", "Himachal Pradesh", "Jharkhand",
                  "Karnataka", "Kerala", "Madhya Pradesh", "Maharashtra", "Manipur", "Meghalaya", "Mizoram", "Nagaland", "Odisha", "Punjab",
                  "Rajasthan", "Sikkim", "Tamil Nadu", "Telangana", "Tripura","Uttar Pradesh", "Uttarakhand", "West Bengal", "Delhi"
            ]

        student_state = st.selectbox(
               "State *",
               states_list,
               key="welcome_state"
           )
    
    st.markdown('<hr>', unsafe_allow_html=True)
    
    # Define grade lists
    school_grades_list = ["9th", "10th", "11th", "12th"]
    college_grades_list = ["1st Year College", "2nd Year College", "3rd Year College", "4th Year College"]
    
    if st.button("🚀 Start Your Career Journey", type="primary", use_container_width=True, key="start_journey"):
        if student_name and student_grade != "Select Grade" and student_city and student_state:
            st.session_state.student_name = student_name
            st.session_state.student_age = student_age
            st.session_state.student_city = student_city
            st.session_state.student_state = student_state
            st.session_state.student_institution = student_institution
            st.session_state.student_grade = student_grade
            
            if student_grade in school_grades_list:
                st.session_state.user_type = "school"
            elif student_grade in college_grades_list:
                st.session_state.user_type = "college"
            else:
                st.session_state.user_type = "school"
            
            st.session_state.page = 'load_assessment'
            st.rerun()
        elif not student_name:
            st.warning("Please enter your name")
        elif not student_city:
            st.warning("Please enter your city")
        elif student_state == "Select State":
            st.warning("Please select your state")
        elif student_grade == "Select Grade":
            st.warning("Please select your grade/year")
    
    st.markdown('</div>', unsafe_allow_html=True)

# Load Assessment Page
def show_load_assessment():
    with st.spinner("Loading your personalized assessment..."):
        # Load appropriate JSON file based on user type
        if st.session_state.user_type == 'school':
            json_file = 'school_questions.json'
        else:
            json_file = 'college_questions.json'
        
        data = load_json_file(json_file)
        
        if data:
            questions, categories = extract_questions_from_json(data)
            st.session_state.questions_list = questions
            st.session_state.categories_data = categories
            st.session_state.responses = {}
            st.session_state.current_page = 0
            st.session_state.selected_stream = None
            st.session_state.recommended_categories = []
            st.session_state.page = 'assessment'
            st.rerun()
        else:
            st.error(f"{json_file} not found. Please make sure the file exists.")
            if st.button("Go Back"):
                st.session_state.page = 'welcome'
                st.rerun()

# Assessment Page - 10 questions per page
def show_assessment():
    show_header()
    st.markdown('<div class="main-card">', unsafe_allow_html=True)
    
    # Show user type indicator
    user_type_display = "School Student" if st.session_state.user_type == 'school' else "College Student"
    st.markdown(f'<div class="user-type-indicator">🎓 {user_type_display} Pathway</div>', unsafe_allow_html=True)
    
    if not st.session_state.questions_list:
        st.error("No questions loaded. Please go back and try again.")
        if st.button("Go Back"):
            st.session_state.page = 'welcome'
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)
        return
    
    total_questions = len(st.session_state.questions_list)
    questions_per_page = 10
    total_pages = (total_questions + questions_per_page - 1) // questions_per_page
    current_page = st.session_state.current_page
    start_idx = current_page * questions_per_page
    end_idx = min(start_idx + questions_per_page, total_questions)
    page_questions = st.session_state.questions_list[start_idx:end_idx]
    
    st.markdown(f'<h1 class="welcome-heading" style="font-size:1.8rem;">Career Assessment</h1>', unsafe_allow_html=True)
    st.markdown(f'<p class="sub-message" style="font-size:1.1rem;">Rate each statement honestly to get accurate career recommendations</p>', unsafe_allow_html=True)
    
    # Page counter
    st.markdown(f'<div class="page-counter">📄 Page {current_page + 1} of {total_pages}</div>', unsafe_allow_html=True)
    
    # Display questions for current page
    for i, q in enumerate(page_questions):
        question_number = start_idx + i + 1
        st.markdown(f"""
        <div style="background: #FEF9F0; border-radius: 20px; padding: 1.2rem; margin: 1rem 0; border-left: 5px solid #E67E22; box-shadow: 0 2px 8px rgba(0,0,0,0.05);">
            <div style="font-weight: 700; color: #2E7D32; margin-bottom: 1rem; font-size: 1.2rem;">{question_number}. {q['text']}</div>
            <p style="color: #1565C0; font-size: 0.85rem; margin-bottom: 0.5rem;">Category: {q['category_name']}</p>
        </div>
        """, unsafe_allow_html=True)
        
        # Radio button with NO default selection (index=None)
        rating = st.radio(
            "Select your answer:",
            options=[1, 2, 3, 4, 5],
            format_func=lambda x: {1: "1 - Strongly Disagree", 2: "2 - Disagree", 3: "3 - Neutral", 4: "4 - Agree", 5: "5 - Strongly Agree"}[x],
            horizontal=True,
            key=f"q_{q['id']}_{question_number}",
            index=0  # This prevents any default selection
        )
        
        # Only store if user made a selection
        if rating is not None:
            st.session_state.responses[q['id']] = rating
        
        st.markdown("<br>", unsafe_allow_html=True)
    
    # Calculate answered questions count
    answered_count = sum(1 for q in st.session_state.questions_list[:end_idx] 
                         if st.session_state.responses.get(q['id']) is not None)
    progress = answered_count / total_questions if total_questions > 0 else 0
    st.progress(progress)
    st.markdown(f'<p class="progress-text" style="font-size:0.9rem;">📊 Progress: {answered_count}/{total_questions} questions answered ({int(progress*100)}%)</p>', unsafe_allow_html=True)
    
    # Navigation buttons
    col1, col2, col3 = st.columns([1, 2, 1])
    with col1:
        if current_page > 0:
            if st.button("← Previous Page", key="career_prev_page"):
                st.session_state.current_page -= 1
                st.rerun()
    
    with col3:
        # Check if all questions on current page are answered
        current_page_answered = all(
            st.session_state.responses.get(q['id']) is not None 
            for q in page_questions
        )
        
        if end_idx < total_questions:
            if current_page_answered:
                if st.button("Next Page →", key="career_next_page", type="primary"):
                    st.session_state.current_page += 1
                    st.rerun()
            else:
                # Show disabled button with warning
                st.button("Next Page →", key="career_next_page_disabled", disabled=True)
                st.warning(f"⚠️ Please answer all {len(page_questions)} questions on this page")
        else:
            # Last page - check if all questions are answered
            all_answered = all(
                st.session_state.responses.get(q['id']) is not None 
                for q in st.session_state.questions_list
            )
            if all_answered:
                if st.button("Submit & Get Results", type="primary", key="submit_career_results"):
                    # Calculate career assessment results
                    st.session_state.categories_data, st.session_state.recommended_categories = calculate_results(
                        st.session_state.responses, st.session_state.questions_list, st.session_state.categories_data
                    )
                    # Go to personality choice (OPTIONAL assessment)
                    st.session_state.page = 'personality_choice'
                    st.rerun()
            else:
                unanswered = total_questions - answered_count
                st.button("Submit & Get Results", key="submit_career_results_disabled", disabled=True)
                st.warning(f"⚠️ Please answer {unanswered} more question(s) before submitting")
    
    st.markdown('</div>', unsafe_allow_html=True)

def show_personality_choice():
    show_header()
    st.markdown('<div class="main-card">', unsafe_allow_html=True)
    
    st.markdown(f'''
    <div class="student-info-card">
        👋 Welcome, <strong>{st.session_state.student_name}</strong><br>
        📍 {st.session_state.student_city} | 🎂 Age: {st.session_state.student_age} | 📚 {st.session_state.student_grade}
    </div>
    ''', unsafe_allow_html=True)
    
    st.markdown('<h1 class="welcome-heading" style="font-size:1.8rem;">🎭 Optional: Personality Assessment</h1>', unsafe_allow_html=True)
    st.markdown('<p class="welcome-subheading">⚠️ <strong>Note: Your career assessment is already complete. This is optional.</strong></p>', unsafe_allow_html=True)
    
    # Add Back button to go to career assessment
    col_back1, col_back2, col_back3 = st.columns([1, 2, 1])
    with col_back1:
        if st.button("←Back   ", key="back_to_career_assessment", use_container_width=True):
            st.session_state.page = 'assessment'
            st.rerun()
    
    st.markdown("<br>", unsafe_allow_html=True)
    
    # ============ DETAILS SHOWN DIRECTLY (NO EXPANDER) ============
    st.markdown("""
    <div style="background: #E8F5E9; border-radius: 16px; padding: 1rem; margin: 1rem 0; border-left: 5px solid #1565C0;">
        <strong style="color: #1565C0; font-size: 1.1rem;">🔍 What does the personality assessment include?</strong><br><br>
        The personality assessment helps identify your learning style by evaluating:
        <ul>
            <li><strong>Learning Preferences</strong>: How you learn best (visual, auditory, reading/writing, kinesthetic)</li>
            <li><strong>Work Style</strong>: Whether you prefer independent work, teamwork, or a mix</li>
            <li><strong>Problem-Solving Approach</strong>: How you tackle challenges and new problems</li>
            <li><strong>Communication Style</strong>: Your preferred way of expressing ideas</li>
            <li><strong>Study Habits</strong>: Effective learning strategies for your personality type</li>
        </ul>
        Based on your responses, we'll provide personalized study tips and career suggestions!
    </div>
    """, unsafe_allow_html=True)
    
    st.markdown("<br>", unsafe_allow_html=True)
    
    # Display assessment options
    st.markdown('<p style="text-align: center; font-size: 1.1rem; font-weight: 500;">Choose an option below:</p>', unsafe_allow_html=True)
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.markdown('''
        <div class="user-card" style="height: 100%; min-height: 350px; display: flex; flex-direction: column; justify-content: space-between;">
            <div>
                <div class="user-icon">📝</div>
                <h3>Take Personality Assessment</h3>
                <p>Complete the personality assessment to discover your learning style and get personalized study tips.</p>
                <p style="margin-top:10px; font-size:0.8rem; color:#1E88E5;">⏱️ Takes about 10-15 minutes</p>
                <p style="font-size:0.8rem; color:#1E88E5;">📊 25 questions</p>
                <p style="margin-top:10px; font-size:0.8rem; color:#1565C0;">✨ Get personalized insights</p>
            </div>
            <div style="margin-top: 15px;">
                <div class="stButton" style="width: 100%;"></div>
            </div>
        </div>
        ''', unsafe_allow_html=True)
        
        if st.button("✅ Yes, Take Personality Assessment", key="take_personality_btn", use_container_width=True):
            st.session_state.personality_questions = get_personality_questions(st.session_state.user_type)
            st.session_state.personality_responses = {}
            st.session_state.personality_current_page = 0
            st.session_state.personality_completed = False
            st.session_state.page = 'personality_assessment'
            st.rerun()
    
    with col2:
        st.markdown('''
        <div class="user-card" style="height: 100%; min-height: 350px; display: flex; flex-direction: column; justify-content: space-between;">
            <div>
                <div class="user-icon">⏭️</div>
                <h3>Skip Personality Assessment</h3>
                <p>Skip the personality test and go directly to your career stream comparison.</p>
                <p style="margin-top:10px; font-size:0.8rem; color:#1E88E5;">⚡ Continue directly</p>
                <p style="font-size:0.8rem; color:#1E88E5;">📊 View your career recommendations</p>
                <p style="margin-top:10px; font-size:0.8rem; color:#1565C0;">🎯 Your career assessment results are ready!</p>
            </div>
            <div style="margin-top: 15px;">
                <div class="stButton" style="width: 100%;"></div>
            </div>
        </div>
        ''', unsafe_allow_html=True)
        
        if st.button("⏭️ Skip Personality Assessment", key="skip_personality_btn", use_container_width=True):
            st.session_state.page = 'recommendation'
            st.rerun()
    
    st.markdown('</div>', unsafe_allow_html=True)

def show_personality_assessment():
    # Add Home button only at the top
    col_home, col_spacer = st.columns([1, 11])
    with col_home:
        if st.button("🏠 Home", key="personality_home", use_container_width=True):
            # Reset to welcome page
            for key in list(st.session_state.keys()):
                if key not in ['page']:
                    del st.session_state[key]
            st.session_state.page = 'welcome'
            st.rerun()
    
    st.markdown('<div class="main-card">', unsafe_allow_html=True)
    
    if not st.session_state.personality_questions:
        st.session_state.personality_questions = get_personality_questions(st.session_state.user_type)
    
    questions_per_page = 10
    total_questions = len(st.session_state.personality_questions)
    total_pages = (total_questions + questions_per_page - 1) // questions_per_page
    current_page = st.session_state.personality_current_page
    start_idx = current_page * questions_per_page
    end_idx = min(start_idx + questions_per_page, total_questions)
    page_questions = st.session_state.personality_questions[start_idx:end_idx]
    
    st.markdown(f'<h1 class="welcome-heading" style="font-size:1.5rem;">🧠 Personality Assessment</h1>', unsafe_allow_html=True)
    st.markdown(f'<p class="sub-message">Page {current_page + 1} of {total_pages} • Discover your learning style</p>', unsafe_allow_html=True)
    
    for i, q in enumerate(page_questions):
        q_index = start_idx + i
        st.markdown(f"""
        <div class="question-card">
            <div class="question-text">{q_index + 1}. {q['text']}</div>
        </div>
        """, unsafe_allow_html=True)
        
        # Get current value from session state if exists
        current_value = st.session_state.personality_responses.get(q['id'], None)
        
        # Radio button with NO default selection (index=None)
        answer = st.radio(
            "Select your answer:",
            options=q['options'],
            horizontal=True,
            key=f"personality_q_{q['id']}_{q_index}",
            index=None  # This removes the default pointer - no option pre-selected
        )
        
        # Only store if user made a selection
        if answer is not None:
            st.session_state.personality_responses[q['id']] = answer
        
        st.markdown("<br>", unsafe_allow_html=True)
    
    # Calculate progress based on answered questions
    answered_count = sum(1 for q in st.session_state.personality_questions[:end_idx] 
                         if st.session_state.personality_responses.get(q['id']) is not None)
    progress = answered_count / total_questions if total_questions > 0 else 0
    st.progress(progress)
    st.markdown(f'<p class="progress-text">📊 Progress: {answered_count}/{total_questions} questions answered ({int(progress*100)}%)</p>', unsafe_allow_html=True)
    
    # Navigation buttons
    col1, col2, col3 = st.columns([1, 2, 1])
    with col1:
        if current_page > 0:
            if st.button("← Previous Page", use_container_width=True):
                st.session_state.personality_current_page -= 1
                st.rerun()
    
    with col3:
        # Check if all questions on current page are answered
        current_page_answered = all(
            st.session_state.personality_responses.get(q['id']) is not None 
            for q in page_questions
        )
        
        if end_idx < total_questions:
            if current_page_answered:
                if st.button("Next Page →", type="primary", use_container_width=True):
                    st.session_state.personality_current_page += 1
                    st.rerun()
            else:
                st.button("Next Page →", type="primary", use_container_width=True, disabled=True)
                st.warning(f"⚠️ Please answer all {len(page_questions)} questions on this page")
        else:
            # Last page - check if all questions are answered
            all_answered = all(
                st.session_state.personality_responses.get(q['id']) is not None 
                for q in st.session_state.personality_questions
            )
            if all_answered:
                if st.button("Submit & See Results", type="primary", use_container_width=True):
                    # Filter out None values before analyzing
                    valid_responses = {k: v for k, v in st.session_state.personality_responses.items() if v is not None}
                    st.session_state.personality_pathway = analyze_personality_pathway(
                        valid_responses, st.session_state.user_type
                    )
                    st.session_state.personality_completed = True
                    st.session_state.page = 'personality_result'
                    st.rerun()
            else:
                unanswered = total_questions - answered_count
                st.button("Submit & See Results", type="primary", use_container_width=True, disabled=True)
                st.warning(f"⚠️ Please answer {unanswered} more question(s) before submitting")
    
    # Skip button with full width
    st.markdown("---")
    col_skip1, col_skip2, col_skip3 = st.columns([1, 2, 1])
    with col_skip2:
        if st.button("⏭️ Skip for Now", use_container_width=True):
            st.session_state.personality_skipped = True
            st.session_state.page = 'recommendation'
            st.rerun()
    
    st.markdown('</div>', unsafe_allow_html=True)

def show_personality_result():
    show_header()
    st.markdown('<div class="main-card">', unsafe_allow_html=True)
    
    if st.session_state.personality_pathway:
        pathway = st.session_state.personality_pathway
        
        st.markdown(f"""
        <div class="stream-detail-card">
            <div style="text-align:center;">
                <div style="font-size:3rem;">{pathway['icon']}</div>
                <h1 style="color:#D35400; margin:0.5rem 0;">{pathway['title']}</h1>
                <div style="background:#FFF3E0; display:inline-block; padding:0.3rem 1rem; border-radius:20px; margin:0.5rem 0;">
                    {pathway['match_percentage']}% Match
                </div>
            </div>
        </div>
        """, unsafe_allow_html=True)
        
        st.markdown(f"<p>{pathway['description']}</p>", unsafe_allow_html=True)
        
        st.markdown("**✨ Your Key Strengths:**")
        for strength in pathway.get('strengths', []):
            st.markdown(f"- {strength}")
        
        st.markdown("<hr>", unsafe_allow_html=True)
        
        col1, col2 = st.columns(2)
        with col1:
            if st.button("← Back to Personality Test", use_container_width=True):
                st.session_state.page = 'personality_assessment'
                st.rerun()
        with col2:
            if st.button("Continue →", type="primary", use_container_width=True):
                st.session_state.page = 'recommendation'
                st.rerun()
    
    st.markdown('</div>', unsafe_allow_html=True)

# Report Page
# ==================== AI RECOMMENDATION PAGE (PLACEHOLDER) ====================
def show_recommendation():
    show_header()
    st.markdown('<div class="main-card">', unsafe_allow_html=True)

    st.markdown('<h1 class="welcome-heading" style="font-size:1.8rem;">🎯 Your Top 3 AI Career Recommendations</h1>', unsafe_allow_html=True)

    student_details = {
        'name': st.session_state.student_name,
        'age': st.session_state.student_age,
        'institution': st.session_state.student_institution,
        'city': st.session_state.student_city,
        'state': st.session_state.student_state,
        'grade': st.session_state.student_grade,
    }

    # Cache by fingerprint: only call Gemini again if the questionnaire /
    # personality answers / user type actually changed since the last call.
    current_fp = make_response_fingerprint(
        st.session_state.responses,
        st.session_state.personality_responses,
        st.session_state.user_type,
    )
    if st.session_state.ai_recommendation is None or st.session_state.ai_recommendation_fingerprint != current_fp:
        with st.spinner("Analysing your responses with AI..."):
            recommendation = generate_ai_recommendation(
                student_details,
                st.session_state.responses,
                st.session_state.personality_responses,
                st.session_state.user_type,
            )
        st.session_state.ai_recommendation = recommendation
        st.session_state.ai_recommendation_fingerprint = current_fp
    else:
        recommendation = st.session_state.ai_recommendation

    streams = st.session_state.ai_top_streams

    if recommendation.get("status") != "success" or not streams:
        st.markdown(f"""
        <div style="background:#FFF3E0; border-radius:16px; padding:1.2rem; margin:1rem 0;">
            <p>⚠️ {recommendation.get('message', 'AI recommendations are not available right now.')}</p>
        </div>
        """, unsafe_allow_html=True)
        if st.session_state.get('gemini_response_error'):
            with st.expander("Technical details (for debugging)"):
                st.code(st.session_state.gemini_response_error)
        if st.button("🔁 Retry AI Analysis", use_container_width=True):
            st.session_state.ai_recommendation = None
            st.session_state.ai_top_streams = None
            st.rerun()
    else:
        card_styles = ["stream-card-high", "stream-card-good", "stream-card-fair"]
        cols = st.columns(3)
        for idx, stream in enumerate(streams[:3]):
            style = card_styles[idx % len(card_styles)]
            with cols[idx]:
                st.markdown(f"""
                <div class="{style}">
                    <div style="font-size:0.85rem; opacity:0.85; font-weight:600;">#{idx + 1} RECOMMENDED</div>
                    <h3 style="margin:0.4rem 0;">{stream['stream_name']}</h3>
                    <div class="score-bar">
                        <div class="score-fill" style="width:{stream['match_percentage']}%;"></div>
                    </div>
                    <div style="font-size:1.1rem; font-weight:700; margin:0.4rem 0;">{stream['match_percentage']}% Match</div>
                    <p style="font-size:0.85rem; line-height:1.4; text-align:left;">{stream['explanation']}</p>
                </div>
                """, unsafe_allow_html=True)
                if st.button(f"Explore →", key=f"explore_{idx}", use_container_width=True):
                    st.session_state.selected_stream = stream['stream_name']
                    st.session_state.selected_stream_data = stream
                    st.session_state.ai_analysis = None
                    st.session_state.ai_deep_dive = None
                    st.session_state.page = 'ai_analysis'
                    st.rerun()

    st.markdown("<hr>", unsafe_allow_html=True)

    col1, col2 = st.columns(2)
    with col1:
        if st.button("← Back to Assessment", use_container_width=True):
            st.session_state.page = 'assessment'
            st.rerun()
    with col2:
        if streams and st.button("Continue to Report →", type="primary", use_container_width=True):
            if not st.session_state.get('selected_stream_data'):
                st.session_state.selected_stream = streams[0]['stream_name']
                st.session_state.selected_stream_data = streams[0]
            st.session_state.ai_analysis = None
            st.session_state.ai_deep_dive = None
            st.session_state.page = 'ai_analysis'
            st.rerun()

    st.markdown('</div>', unsafe_allow_html=True)


def show_ai_analysis():
    show_header()
    st.markdown('<div class="main-card">', unsafe_allow_html=True)

    stream = st.session_state.get('selected_stream_data')
    if not stream:
        st.warning("No stream selected yet. Please go back and choose a recommendation.")
        if st.button("← Back to Recommendations", use_container_width=True):
            st.session_state.page = 'recommendation'
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)
        return

    st.markdown(f'<h1 class="welcome-heading" style="font-size:1.8rem;">🧠 AI Analysis</h1>', unsafe_allow_html=True)
    st.markdown(f'<p class="sub-message" style="text-align:center;">{stream["stream_name"]}</p>', unsafe_allow_html=True)

    student_details = {
        'name': st.session_state.student_name,
        'age': st.session_state.student_age,
        'institution': st.session_state.student_institution,
        'city': st.session_state.student_city,
        'state': st.session_state.student_state,
        'grade': st.session_state.student_grade,
    }

    current_fp = make_response_fingerprint(
        st.session_state.responses,
        st.session_state.user_type,
        stream['stream_name'],
    )
    if st.session_state.ai_analysis is None or st.session_state.ai_analysis_fingerprint != current_fp:
        with st.spinner("Generating your personalized AI Analysis..."):
            result = generate_ai_analysis(
                student_details,
                st.session_state.responses,
                stream['stream_name'],
                st.session_state.user_type,
            )
        st.session_state.ai_analysis_status = result
        st.session_state.ai_analysis_fingerprint = current_fp

    analysis = st.session_state.ai_analysis

    if not analysis:
        msg = (st.session_state.ai_analysis_status or {}).get(
            'message', 'AI Analysis could not be generated right now.'
        )
        st.markdown(f"""
        <div style="background:#FFF3E0; border-radius:16px; padding:1.2rem; margin:1rem 0;">
            <p>⚠️ {msg}</p>
        </div>
        """, unsafe_allow_html=True)
        if st.session_state.get('ai_analysis_error'):
            with st.expander("Technical details (for debugging)"):
                st.code(st.session_state.ai_analysis_error)
        if st.button("🔁 Retry AI Analysis", use_container_width=True):
            st.session_state.ai_analysis = None
            st.rerun()
    else:
        col1, col2 = st.columns(2)
        with col1:
            st.markdown('<div class="stream-card-high" style="text-align:left;">', unsafe_allow_html=True)
            st.markdown('<h3>💪 Strengths</h3>', unsafe_allow_html=True)
            for s in analysis['strengths']:
                st.markdown(f"- {s}")
            st.markdown('</div>', unsafe_allow_html=True)
        with col2:
            st.markdown('<div class="stream-card-potential" style="text-align:left;">', unsafe_allow_html=True)
            st.markdown('<h3>🚀 Opportunities</h3>', unsafe_allow_html=True)
            for o in analysis['opportunities']:
                st.markdown(f"- {o}")
            st.markdown('</div>', unsafe_allow_html=True)

    st.markdown("<hr>", unsafe_allow_html=True)

    col1, col2 = st.columns(2)
    with col1:
        if st.button("← Back to Recommendations", use_container_width=True):
            st.session_state.page = 'recommendation'
            st.rerun()
    with col2:
        if analysis and st.button("Continue to Full Report →", type="primary", use_container_width=True):
            st.session_state.ai_deep_dive = None
            st.session_state.page = 'report'
            st.rerun()

    st.markdown('</div>', unsafe_allow_html=True)


def show_role_detail():
    show_header()
    st.markdown('<div class="main-card">', unsafe_allow_html=True)

    role_name = st.session_state.get('selected_career_role')
    stream = st.session_state.get('selected_stream_data')

    if not role_name or not stream:
        st.warning("No career role selected yet. Please go back to your report.")
        if st.button("← Back to Report", use_container_width=True):
            st.session_state.page = 'report'
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)
        return

    st.markdown(f'<h1 class="welcome-heading" style="font-size:1.7rem;">🧭 {role_name}</h1>', unsafe_allow_html=True)
    st.markdown(f'<p class="sub-message" style="text-align:center;">within {stream["stream_name"]}</p>', unsafe_allow_html=True)

    student_details = {
        'name': st.session_state.student_name,
        'age': st.session_state.student_age,
        'institution': st.session_state.student_institution,
        'city': st.session_state.student_city,
        'state': st.session_state.student_state,
        'grade': st.session_state.student_grade,
    }

    current_fp = make_response_fingerprint(
        st.session_state.user_type,
        stream['stream_name'],
        role_name,
    )
    if st.session_state.ai_role_detail is None or st.session_state.ai_role_detail_fingerprint != current_fp:
        with st.spinner(f"Generating AI breakdown for {role_name}..."):
            result = generate_ai_role_detail(
                student_details,
                role_name,
                stream['stream_name'],
                st.session_state.user_type,
            )
        st.session_state.ai_role_detail_status = result
        st.session_state.ai_role_detail_fingerprint = current_fp

    detail = st.session_state.ai_role_detail

    st.markdown("<hr>", unsafe_allow_html=True)

    if not detail:
        msg = (st.session_state.ai_role_detail_status or {}).get(
            'message', 'This role breakdown could not be generated right now.'
        )
        st.markdown(f"""
        <div style="background:#FFF3E0; border-radius:16px; padding:1.2rem; margin:1rem 0;">
            <p>⚠️ {msg}</p>
        </div>
        """, unsafe_allow_html=True)
        if st.session_state.get('ai_role_detail_error'):
            with st.expander("Technical details (for debugging)"):
                st.code(st.session_state.ai_role_detail_error)
        if st.button("🔁 Retry", use_container_width=True):
            st.session_state.ai_role_detail = None
            st.rerun()
    else:
        def section(title, icon, body):
            st.markdown(f'<div class="question-card" style="text-align:left;">', unsafe_allow_html=True)
            st.markdown(f"**{icon} {title}**")
            if isinstance(body, list):
                for item in body:
                    st.markdown(f"- {item}")
            else:
                st.markdown(body)
            st.markdown('</div>', unsafe_allow_html=True)

        section("Career Description", "📘", detail.get("career_description", ""))
        section("Salary Range (India)", "💰", detail.get("salary_range_india", ""))
        section("Educational Requirements", "🎓", detail.get("educational_requirements", []))
        section("Job Responsibilities", "🧩", detail.get("job_responsibilities", []))
        section("Required Skills", "🛠️", detail.get("required_skills", []))
        section("Future Job Growth", "📈", detail.get("future_job_growth", ""))
        section("Industry Outlook", "🏭", detail.get("industry_outlook", ""))
        section("Top Hiring Companies", "🏢", detail.get("top_hiring_companies", []))

    st.markdown("<hr>", unsafe_allow_html=True)

    if st.button("← Back to Report", use_container_width=True):
        st.session_state.page = 'report'
        st.rerun()

    st.markdown('</div>', unsafe_allow_html=True)


def show_report():
    show_header()
    st.markdown('<div class="main-card">', unsafe_allow_html=True)
    
    st.markdown(f'<h1 class="welcome-heading" style="font-size:1.5rem;">Your Personalized Career Report</h1>', unsafe_allow_html=True)
    st.markdown(f'<p class="sub-message">Prepared for {st.session_state.student_name}</p>', unsafe_allow_html=True)

    stream = st.session_state.get('selected_stream_data')

    if not stream:
        st.warning("No stream selected yet. Please go back and choose a recommendation.")
        if st.button("← Back to Recommendations", use_container_width=True):
            st.session_state.page = 'recommendation'
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)
        return

    st.markdown(f"""
    <div style="background:#FFF8F0; border-radius:16px; padding:1.2rem;">
        <strong>Selected Stream:</strong> {stream['stream_name']}<br>
        <strong>Match Score:</strong> {stream['match_percentage']}%<br><br>
        <p>{stream['explanation']}</p>
    </div>
    """, unsafe_allow_html=True)

    student_details = {
        'name': st.session_state.student_name,
        'age': st.session_state.student_age,
        'institution': st.session_state.student_institution,
        'city': st.session_state.student_city,
        'state': st.session_state.student_state,
        'grade': st.session_state.student_grade,
    }

    current_fp = make_response_fingerprint(
        st.session_state.responses,
        st.session_state.user_type,
        stream['stream_name'],
    )
    if st.session_state.ai_deep_dive is None or st.session_state.ai_deep_dive_fingerprint != current_fp:
        with st.spinner("Generating your full AI career report..."):
            result = generate_ai_deep_dive(
                student_details,
                st.session_state.responses,
                stream['stream_name'],
                st.session_state.user_type,
            )
        st.session_state.ai_deep_dive_status = result
        st.session_state.ai_deep_dive_fingerprint = current_fp

    report = st.session_state.ai_deep_dive

    st.markdown("<hr>", unsafe_allow_html=True)

    if not report:
        msg = (st.session_state.ai_deep_dive_status or {}).get(
            'message', 'The full AI report could not be generated right now.'
        )
        st.markdown(f"""
        <div style="background:#FFF3E0; border-radius:16px; padding:1.2rem; margin:1rem 0;">
            <p>⚠️ {msg}</p>
        </div>
        """, unsafe_allow_html=True)
        if st.session_state.get('ai_deep_dive_error'):
            with st.expander("Technical details (for debugging)"):
                st.code(st.session_state.ai_deep_dive_error)
        if st.button("🔁 Retry Report Generation", use_container_width=True):
            st.session_state.ai_deep_dive = None
            st.rerun()
    else:
        def section(title, icon, body):
            st.markdown(f'<div class="question-card" style="text-align:left;">', unsafe_allow_html=True)
            st.markdown(f"**{icon} {title}**")
            if isinstance(body, list):
                for item in body:
                    st.markdown(f"- {item}")
            else:
                st.markdown(body)
            st.markdown('</div>', unsafe_allow_html=True)

        section("Career Overview", "📘", report.get("career_overview", ""))
        section("Future Scope", "📈", report.get("future_scope", ""))
        section("Technical Skills", "🛠️", report.get("technical_skills", []))
        section("Soft Skills", "🤝", report.get("soft_skills", []))
        section("Major Hiring Cities in India", "🏙️", report.get("major_hiring_cities_india", []))
        section("Education Path", "🎓", report.get("education_path", ""))
        section("Certifications", "📜", report.get("certifications", []))
        section("Learning Resources", "📚", report.get("learning_resources", []))

        roles = report.get("related_career_roles", [])
        if roles:
            st.markdown(f'<div class="question-card" style="text-align:left;">', unsafe_allow_html=True)
            st.markdown(f"**🧭 Related Career Roles**")
            st.markdown("Click a role to see its full AI-generated breakdown.")
            st.markdown('</div>', unsafe_allow_html=True)
            role_cols = st.columns(3)
            for r_idx, role in enumerate(roles):
                with role_cols[r_idx % 3]:
                    if st.button(role, key=f"role_{r_idx}", use_container_width=True):
                        st.session_state.selected_career_role = role
                        st.session_state.ai_role_detail = None
                        st.session_state.page = 'role_detail'
                        st.rerun()

    st.markdown("<hr>", unsafe_allow_html=True)
    
    # ONLY PDF Download button (removed TXT and Print buttons)
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        if st.button("📥 Download Report (PDF)", type="primary", use_container_width=True):
            if st.session_state.selected_stream:
                with st.spinner("Generating PDF report..."):
                    try:
                        pdf_path = generate_pdf_report()
                        with open(pdf_path, "rb") as pdf_file:
                            pdf_data = pdf_file.read()
                        st.download_button(
                            label="💾 Save PDF Report",
                            data=pdf_data,
                            file_name=f"{st.session_state.student_name}_career_report.pdf",
                            mime="application/pdf",
                            key="download_pdf"
                        )
                        os.unlink(pdf_path)
                        st.success("✅ PDF Report generated successfully!")
                    except Exception as e:
                        st.error(f"Error generating PDF: {str(e)}")
    
    st.markdown("<br>", unsafe_allow_html=True)
    
    col1, col2 = st.columns(2)
    with col1:
        if st.button("← Back to AI Analysis", use_container_width=True):
            st.session_state.page = 'ai_analysis'
            st.rerun()
    with col2:
        if st.button("🏠 Start New Assessment", use_container_width=True):
            # Reset all session state
            for key in list(st.session_state.keys()):
                if key not in ['page']:
                    del st.session_state[key]
            st.session_state.page = 'welcome'
            st.rerun()
    
    st.markdown('</div>', unsafe_allow_html=True)

# Main
def main():
    if st.session_state.page == 'welcome':
        show_welcome()
    elif st.session_state.page == 'load_assessment':
        show_load_assessment()
    elif st.session_state.page == 'assessment':
        show_assessment()
    elif st.session_state.page == 'personality_choice':
        show_personality_choice()
    elif st.session_state.page == 'personality_assessment':
        show_personality_assessment()
    elif st.session_state.page == 'personality_result':
        show_personality_result()
    elif st.session_state.page == 'recommendation':
        show_recommendation()
    elif st.session_state.page == 'ai_analysis':
        show_ai_analysis()
    elif st.session_state.page == 'report':
        show_report()
    elif st.session_state.page == 'role_detail':
        show_role_detail()

if __name__ == "__main__":
    main()
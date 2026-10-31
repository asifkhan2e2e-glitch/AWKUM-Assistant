"""
AWKUM Assistant - the web page (Phase 6).

Run on your computer:   streamlit run app/streamlit_app.py        (or: streamlit run chatbot08.py)
The Groq key comes from .env, an environment variable, or Streamlit's secrets (GROQ_API_KEY).
"""
import html, os, random, re, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent if HERE.name == "app" else HERE
for p in (HERE, ROOT / "bot", ROOT / "indexer", ROOT):
    sys.path.insert(0, str(p))

try:                                                   # Streamlit Cloud keeps settings in "secrets"
    for _name in ("GROQ_API_KEY", "CAMPUS_LATLNG"):
        if _name in st.secrets:
            os.environ.setdefault(_name, str(st.secrets[_name]))
except Exception:
    pass

try:
    import brain
except ImportError:                                    # flat folder where the file was renamed
    import chatbot07 as brain

@st.cache_resource(show_spinner="Preparing the data…")
def ensure_index():
    """On a fresh server the search index may not exist yet: build it from data/docs once."""
    if Path(brain.DB).exists():
        return True
    try:
        try:
            import build_index
        except ImportError:
            import chatbot04 as build_index
        build_index.main()
    except BaseException:                              # the builder exits with a message when there is no data
        return False
    return Path(brain.DB).exists()


st.set_page_config(page_title="AWKUM Assistant", page_icon="🎓", layout="centered")

AVATAR = {"assistant": "🎓", "user": "🙂"}
ARABIC = re.compile(r"[؀-ۿݐ-ݿ]")
GREETINGS = {
    "morning": ["Good morning! How can I help you today?", "Good morning! What would you like to know about AWKUM?",
                "Rise and shine! Ask me anything about the university."],
    "afternoon": ["Good afternoon! How can I help you?", "Good afternoon! What can I find for you?",
                  "Hello! Ask me anything about AWKUM."],
    "evening": ["Good evening! How can I help you?", "Good evening! What would you like to know?",
                "Hello! Ask me anything about admissions, fees or merit lists."],
    "night": ["Still up? Ask me anything about AWKUM.", "Hello! How can I help you tonight?",
              "Good evening! What can I look up for you?"],
}
SUGGESTIONS = [("💰 Fee structure", "What is the fee structure?"), ("📋 Merit lists", "Where can I find the latest merit lists?"),
               ("🎓 Admissions", "How can I apply for admission?"), ("🏛️ Departments", "Which faculties and departments does AWKUM have?"),
               ("🏅 Scholarships", "What scholarships are available?"), ("🔐 MIS login", "How do I open the MIS portal?")]

st.markdown("""
<style>
.block-container {max-width: 760px; padding-top: 2rem;}
.hero {text-align: center; margin: 2.2rem 0 1.4rem;}
.hero h1 {font-size: 2rem; margin-bottom: .2rem;}
.hero p {opacity: .7; margin: 0;}
.greet {text-align: center; font-size: 1.35rem; margin: 1.2rem 0 1.4rem;}
.rtl {direction: rtl; text-align: right; line-height: 1.9; font-size: 1.05rem;}
.src a {font-size: .85rem;}
</style>""", unsafe_allow_html=True)


def pk_now():
    return datetime.now(timezone.utc) + timedelta(hours=5)             # Pakistan time


def greeting():
    h = pk_now().hour
    slot = "morning" if 5 <= h < 12 else "afternoon" if 12 <= h < 17 else "evening" if 17 <= h < 22 else "night"
    return random.choice(GREETINGS[slot])


def rtl_html(text):
    """Urdu / Pashto answers must read right-to-left."""
    t = html.escape(text)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"(https?://[^\s<)]+)", r'<a href="\1" target="_blank">\1</a>', t)
    return f'<div class="rtl">{t.replace(chr(10), "<br>")}</div>'


def show(msg):
    if msg["role"] == "assistant" and ARABIC.search(msg["content"]):
        st.markdown(rtl_html(msg["content"]), unsafe_allow_html=True)
    else:
        st.markdown(msg["content"])
    gmap = msg.get("map")
    if gmap:
        components.html(f'<iframe src="{html.escape(gmap["embed"])}" width="100%" height="320" style="border:0;border-radius:12px" '
                        f'loading="lazy" allowfullscreen referrerpolicy="no-referrer-when-downgrade"></iframe>', height=330)
        st.markdown(f"[📍 Open the route in Google Maps]({gmap['url']})")
    if msg.get("sources"):
        with st.expander("Sources"):
            for title, url in msg["sources"]:
                st.markdown(f"- {title}: {url}")


def transcribe(audio):
    """Voice question -> text with Groq Whisper (free)."""
    import requests
    key = brain.load_key()
    models = [m for m in brain.available_models() if "whisper" in m]
    model = "whisper-large-v3" if "whisper-large-v3" in models else (models[0] if models else "whisper-large-v3")
    r = requests.post("https://api.groq.com/openai/v1/audio/transcriptions", headers={"Authorization": f"Bearer {key}"},
                      files={"file": ("voice.wav", audio.getvalue() if hasattr(audio, "getvalue") else audio.read(), "audio/wav")}, data={"model": model, "temperature": 0}, timeout=(10, 60))
    r.raise_for_status()
    return r.json().get("text", "").strip()


if not ensure_index():
    st.error("The university data is not ready yet. Please try again in a few minutes.")
    st.stop()

# ---------- state ----------
if "messages" not in st.session_state:
    st.session_state.messages = []
if "greet" not in st.session_state:
    st.session_state.greet = greeting()
if "pending" not in st.session_state:
    st.session_state.pending = None

with st.sidebar:
    st.header("AWKUM Assistant")
    st.caption("Abdul Wali Khan University Mardan - Garden Campus")
    if st.button("New chat", use_container_width=True):
        st.session_state.messages, st.session_state.greet, st.session_state.pending = [], greeting(), None
        st.rerun()
    st.markdown("**Useful links**")
    st.markdown("- [Official website](https://awkum.edu.pk/)\n- [News and notices](https://news.awkum.edu.pk/)\n- [MIS portal](https://eservices.awkum.edu.pk/)")

# ---------- layout: chat on top, (fallback) voice box below it ----------
chat_area = st.container()
voice_area = st.container()

# ---------- input: the search bar has its own microphone button when this Streamlit version supports it ----------
audio, mic_in_bar = None, True
try:
    value = st.chat_input("Ask anything…", accept_audio=True)
except TypeError:                                      # older Streamlit: plain bar + a voice box under the chat
    value, mic_in_bar = st.chat_input("Ask anything…"), False
def _field(v, name):
    got = getattr(v, name, None)
    return got if got is not None else (v.get(name) if hasattr(v, "get") else None)


question = value if isinstance(value, str) or value is None else (_field(value, "text") or None)
if value is not None and not isinstance(value, str):
    audio = _field(value, "audio")
if not mic_in_bar and hasattr(st, "audio_input"):
    with voice_area:
        audio = st.audio_input("🎤 Ask with your voice", key=f"voice{st.session_state.get('voice_n', 0)}")
if audio is not None and not question:
    try:
        with st.spinner("Converting your voice to text…"):
            question = transcribe(audio)                # the question is shown as TEXT, not as a recording
    except Exception:
        st.warning("Sorry, I could not understand the recording. Please try again or type your question.")
    st.session_state.voice_n = st.session_state.get("voice_n", 0) + 1     # clears the voice box for the next time
question = question or st.session_state.pop("pending", None)
st.session_state.pending = None

with chat_area:
    if not st.session_state.messages and not question:
        st.markdown('<div class="hero"><h1>🎓 AWKUM Assistant</h1><p>Abdul Wali Khan University Mardan</p></div>', unsafe_allow_html=True)
        st.markdown(f'<div class="greet">{html.escape(st.session_state.greet)}</div>', unsafe_allow_html=True)
        cols = st.columns(3)
        for i, (label, suggestion) in enumerate(SUGGESTIONS):
            if cols[i % 3].button(label, key=f"sug{i}", use_container_width=True):
                st.session_state.pending = suggestion
                st.rerun()
    for m in st.session_state.messages:
        with st.chat_message(m["role"], avatar=AVATAR[m["role"]]):
            show(m)

with chat_area:
    if question:
        history = [{"role": m["role"], "content": m["content"]} for m in st.session_state.messages]
        st.session_state.messages.append({"role": "user", "content": question})
        with st.chat_message("user", avatar=AVATAR["user"]):
            st.markdown(question)
        with st.chat_message("assistant", avatar=AVATAR["assistant"]):
            slot = st.empty()                                  # the loading box lives here and is removed when the answer is ready
            with slot.container(), st.status("Understanding your question…", expanded=False) as status:   # spinning circle + current step
                try:
                    result = brain.ask(question, history, progress=lambda text: status.update(label=text))
                    status.update(label="Done", state="complete")
                    msg = {"role": "assistant", "content": result["answer"], "sources": result["sources"], "map": result.get("map")}
                except brain.BrainError as e:
                    status.update(label="Something went wrong", state="error")
                    msg = {"role": "assistant", "content": f"Sorry, I could not answer right now. {e}", "sources": []}
                except Exception:
                    status.update(label="Something went wrong", state="error")
                    msg = {"role": "assistant", "content": "Sorry, something went wrong. Please try again in a moment.", "sources": []}
            slot.empty()
            show(msg)
        st.session_state.messages.append(msg)

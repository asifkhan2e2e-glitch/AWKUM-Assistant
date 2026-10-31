# AWKUM Assistant

Free chatbot for Abdul Wali Khan University Mardan (Garden Campus). Answers from the official AWKUM website
in English, Urdu, Roman Urdu and Pashto.

| File | What it does |
|---|---|
| `chatbot03.py` | reads the website pages and news into `data/docs` |
| `chatbot04.py` | builds the search index `data/index.sqlite` |
| `chatbot05.py` | searches the index |
| `chatbot07.py` | the brain: understands the question, searches, asks Groq, adds links and maps |
| `chatbot08.py` | the web page (Streamlit) |
| `.github/workflows/update-data.yml` | updates `data/` automatically every 6 hours |

Run locally: `python chatbot03.py`, `python chatbot04.py`, `streamlit run chatbot08.py`
(needs a `.env` file with `GROQ_API_KEY=...` and `CAMPUS_LATLNG=lat,lng`; never upload `.env`).

Deploy: Streamlit Community Cloud -> main file `chatbot08.py` -> Secrets: `GROQ_API_KEY`, `CAMPUS_LATLNG`.

# Timetable Builder

A guided web app that builds a complete, provably clash-free school
timetable from step-by-step questions — rooms, subjects, teachers, year
groups — checks every requirement in plain English, solves with Google
OR-Tools CP-SAT, verifies the result with an independent checker, and
hands back Excel/PDF timetables for the whole school.

Run locally:

    pip install -r requirements.txt
    streamlit run streamlit_app.py

Deployed on Streamlit Community Cloud. Python 3.12 recommended.

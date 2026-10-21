# Resume Creator

    pip install -r requirements.txt
    cp .env.example .env      # optional: pre-fill API keys
    streamlit run app.py      # visual UI
    python main.py            # headless CLI

Tip: start in **Preview mode** (turn off "Publish") with small limits to test prompts cheaply.
Google sign-in needs an OAuth *Desktop app* `client_secret.json` (upload it in the sidebar).

Scraping uses `blackfalcondata/linkedin-job-scraper`. The default search is entry-level,
full-time, onsite software engineer jobs in the United States posted in the last
24 hours, capped at 10 results. Change the filters in the sidebar or in `Config`
for CLI runs. Full detail enrichment is enabled so OpenAI receives job descriptions.
Jobs without descriptions are reported as errors without an OpenAI call.
The scraper output is normalized before duplicate checking, resume generation,
and spreadsheet logging. Set the limit low for your first run; enrichment adds work.

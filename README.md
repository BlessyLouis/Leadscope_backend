# Company Enrichment API

## Setup

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=your_key_here
python app.py
```

## Endpoints

### POST /enrich
```json
{ "url": "https://example.com", "website_name": "Example Co" }
```

### GET /results
Returns all enriched companies.

## Deploy to Render
1. Push to GitHub
2. Create new Web Service on render.com
3. Set ANTHROPIC_API_KEY env var
4. Deploy

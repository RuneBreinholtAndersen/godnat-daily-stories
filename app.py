import os
import base64
import json
import io
import datetime

from flask import Flask, jsonify
import requests
from PIL import Image
from openai import OpenAI

app = Flask(__name__)

# OpenAI klient - kræver OPENAI_API_KEY som env var
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# WordPress konfiguration
WP_URL = "https://godnathistorierforborn.dk"
WP_USER = os.environ.get("WP_USER")
WP_APP_PASSWORD = os.environ.get("WP_APP_PASSWORD")

# kategori-id'er fra WordPress
CATEGORY_IDS = {
    "1-2 minutter": 4,
    "3-5 minutter": 5,
    "Eventyr": 7
}


# ------------------------------------------------------
# HJÆLPEFUNKTIONER
# ------------------------------------------------------

def get_wp_auth_header():
    if not WP_USER or not WP_APP_PASSWORD:
        raise ValueError("WP_USER eller WP_APP_PASSWORD er ikke sat som environment variables.")
    token = base64.b64encode(f"{WP_USER}:{WP_APP_PASSWORD}".encode("utf-8")).decode("utf-8")
    return {"Authorization": f"Basic {token}"}


# ------------------------------------------------------
# GENERER HISTORIE (GPT)
# ------------------------------------------------------

def generate_story_with_gpt():
    system_prompt = """
Du er en dansk børnebogsforfatter, der skriver varme, fantasifulde godnathistorier for børn i alderen ca. 4-9 år.
Du SKAL svare i ren JSON uden forklarende tekst.

JSON-strukturen skal være:

{
  "title": "...",
  "slug": "...",
  "seo_title": "...",
  "meta_description": "...",
  "category": "1-2 minutter" | "3-5 minutter" | "Eventyr",
  "story_html": "...",
  "image_prompt": "..."
}

Krav:
- Skriv en HELT ny historie, ikke genbrug.
- Sprog: naturligt, flydende dansk.
- Stil: varm, nærværende og let magisk, som historierne på godnathistorierforborn.dk.
- Ingen for voldsomme eller uhyggelige elementer, kun mild spænding.
- Historien skal være tydeligt afsluttet.
- Brug korte afsnit og <p>-tags i story_html.
- title: Kort og fængende.
- slug: pæn URL-slug i små bogstaver.
- meta_description: ca. 140-150 tegn.
- category:
    - "1-2 minutter" for ca. 150-250 ord.
    - "3-5 minutter" for ca. 250-450 ord.
    - "Eventyr" for længere historier.
- image_prompt: ENGELSK prompt i børnetegningsstil, 16:9
"""

    user_prompt = "Lav en ny dansk godnathistorie og svar KUN med gyldig JSON."

    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        temperature=0.9
    )

    content = resp.choices[0].message.content.strip()

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        raise ValueError("Kunne ikke parse JSON fra GPT: " + content[:500])

    for key in ["title", "slug", "meta_description", "story_html", "category", "image_prompt"]:
        if key not in data:
            raise ValueError(f"JSON mangler felt '{key}'")

    return data


# ------------------------------------------------------
# GENERER AI-BILLEDE
# ------------------------------------------------------

def generate_image(image_prompt):
    img_resp = client.images.generate(
        model="gpt-image-1",
        prompt=image_prompt,
        size="1536x1024",
        n=1
    )

    b64 = img_resp.data[0].b64_json
    raw = base64.b64decode(b64)

    with Image.open(io.BytesIO(raw)) as im:
        target_w, target_h = 1200, 630
        w, h = im.size
        target_ratio = target_w / target_h
        current_ratio = w / h

        # crop til 1200x630 format
        if current_ratio > target_ratio:
            new_w = int(h * target_ratio)
            left = (w - new_w) // 2
            right = left + new_w
            top = 0
            bottom = h
        else:
            new_h = int(w / target_ratio)
            top = (h - new_h) // 2
            bottom = top + new_h
            left = 0
            right = w

        cropped = im.crop((left, top, right, bottom))
        resized = cropped.resize((target_w, target_h), Image.LANCZOS)

        out = io.BytesIO()
        resized.save(out, format="JPEG", quality=90)
        out.seek(0)
        return out.read()


# ------------------------------------------------------
# UPLOAD MEDIE TIL WORDPRESS
# ------------------------------------------------------

def upload_image_to_wordpress(img_bytes, filename="og-image.jpg"):
    headers = get_wp_auth_header()
    headers.update({
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Type": "image/jpeg"
    })

    url = f"{WP_URL}/wp-json/wp/v2/media"
    resp = requests.post(url, headers=headers, data=img_bytes)

    if resp.status_code not in (200, 201):
        raise ValueError(f"Fejl ved upload af billede: {resp.status_code} {resp.text}")

    return resp.json()["id"]


# ------------------------------------------------------
# OPRET WP INDLÆG
# ------------------------------------------------------

def create_post_in_wordpress(story_data, featured_media_id):
    headers = get_wp_auth_header()
    headers["Content-Type"] = "application/json"

    category_name = story_data["category"]
    category_id = CATEGORY_IDS.get(category_name, CATEGORY_IDS["3-5 minutter"])

    payload = {
        "title": story_data["title"],
        "slug": story_data["slug"],
        "content": story_data["story_html"],
        "status": "publish",
        "categories": [category_id],
        "featured_media": featured_media_id,
        "excerpt": story_data["meta_description"]
    }

    url = f"{WP_URL}/wp-json/wp/v2/posts"
    resp = requests.post(url, headers=headers, data=json.dumps(payload))

    if resp.status_code not in (200, 201):
        raise ValueError(f"Fejl ved oprettelse af indlæg: {resp.status_code} {resp.text}")

    return resp.json()


# ------------------------------------------------------
# KØR HELE FLOWET
# ------------------------------------------------------

def generate_story_and_post():
    story_data = generate_story_with_gpt()
    img_bytes = generate_image(story_data["image_prompt"])
    media_id = upload_image_to_wordpress(img_bytes)
    post = create_post_in_wordpress(story_data, media_id)
    return post


# ------------------------------------------------------
# FAILSAFE: MAX 1 HISTORIE / 24 TIMER
# ------------------------------------------------------

@app.route("/run-daily", methods=["GET"])
def run_daily():

    option_name = "ai_daily_last_run"
    option_url = f"{WP_URL}/wp-json/wp/v2/options/{option_name}"

    # Hent sidste kørsel
    try:
        opt = requests.get(option_url, auth=(WP_USER, WP_APP_PASSWORD))
        if opt.status_code == 200:
            last_run_str = opt.json().get("value")
        else:
            last_run_str = None
    except:
        last_run_str = None

    # Sammenlign tider
    if last_run_str:
        try:
            last_run = datetime.datetime.fromisoformat(last_run_str.replace("Z", "+00:00"))
            now = datetime.datetime.utcnow()
            diff = (now - last_run).total_seconds()

            if diff < 86400:  # 24 timer
                return jsonify({
                    "status": "skipped",
                    "message": "Der er allerede udgivet en historie inden for 24 timer."
                })
        except:
            pass

    # Generer ny historie
    try:
        post = generate_story_and_post()
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

    # Opdater tidspunkt
    now_str = datetime.datetime.utcnow().isoformat() + "Z"

    requests.post(
        f"{WP_URL}/wp-json/wp/v2/options",
        auth=(WP_USER, WP_APP_PASSWORD),
        json={
            "name": option_name,
            "value": now_str
        }
    )

    return jsonify({
        "status": "ok",
        "message": "Ny historie publiceret.",
        "post_id": post.get("id"),
        "link": post.get("link")
    })


@app.route("/", methods=["GET"])
def index():
    return "AI daily story service kører."


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)

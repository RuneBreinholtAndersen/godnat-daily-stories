import os
import base64
import json
import io

from flask import Flask, jsonify
import requests
from PIL import Image
from openai import OpenAI

app = Flask(__name__)

# OpenAI klient - kræver OPENAI_API_KEY som env var
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# konfiguration
WP_URL = "https://godnathistorierforborn.dk"
WP_USER = os.environ.get("WP_USER")              # fx "Rune"
WP_APP_PASSWORD = os.environ.get("WP_APP_PASSWORD")

# kategori-id'er fra WordPress
CATEGORY_IDS = {
    "1-2 minutter": 4,
    "3-5 minutter": 5,
    "Eventyr": 7
}

def get_wp_auth_header():
    if not WP_USER or not WP_APP_PASSWORD:
        raise ValueError("WP_USER eller WP_APP_PASSWORD er ikke sat som environment variables.")
    token = base64.b64encode(f"{WP_USER}:{WP_APP_PASSWORD}".encode("utf-8")).decode("utf-8")
    return {"Authorization": f"Basic {token}"}

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
- title: Kort og fængende, fx "Emilie og Den Hemmelige Regnbuebutik".
- seo_title: Må gerne være lig title eller fx "TITLE - godnathistorie for børn".
- meta_description: max ca. 140-150 tegn, naturlig og appetitlig beskrivelse.
- slug: lav en pæn slug i små bogstaver med bindestreger.
- category:
    - "1-2 minutter" for ca. 150-250 ord.
    - "3-5 minutter" for ca. 250-450 ord.
    - "Eventyr" hvis historien er længere eller mere eventyr-agtig.
- image_prompt: Skriv en ENGELSK prompt til et AI-billede i 16:9,
  samme børnetegnings-stil som illustrationen med barnet og det lille støvfnug:
  - warm colored pencil illustration
  - closeup of child and cute magical creature
  - soft bedtime lighting
  - children's book style
  - cozy, friendly atmosphere
"""
    user_prompt = "Lav en ny dansk godnathistorie for børn i den stil. Husk: svar KUN som gyldig JSON."

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
        raise ValueError("Kunne ikke parse JSON-svar fra GPT. Svar var: " + content[:500])

    for key in ["title", "slug", "meta_description", "story_html", "category", "image_prompt"]:
        if key not in data:
            raise ValueError(f"Mangler felt '{key}' i GPT-svar.")
    return data

def generate_image(image_prompt):
    img_resp = client.images.generate(
        model="gpt-image-1",
        prompt=image_prompt,
        size="1536x1024",
        n=1
    )

    b64_data = img_resp.data[0].b64_json
    img_bytes = base64.b64decode(b64_data)

    with Image.open(io.BytesIO(img_bytes)) as im:
        target_width, target_height = 1200, 630
        target_ratio = target_width / target_height

        w, h = im.size
        current_ratio = w / h

        if current_ratio > target_ratio:
            new_width = int(h * target_ratio)
            left = (w - new_width) // 2
            right = left + new_width
            top = 0
            bottom = h
        else:
            new_height = int(w / target_ratio)
            top = (h - new_height) // 2
            bottom = top + new_height
            left = 0
            right = w

        im_cropped = im.crop((left, top, right, bottom))
        im_resized = im_cropped.resize((target_width, target_height), Image.LANCZOS)

        out_buf = io.BytesIO()
        im_resized.save(out_buf, format="JPEG", quality=90)
        out_buf.seek(0)
        return out_buf.read()

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

    media_data = resp.json()
    return media_data["id"]

def create_post_in_wordpress(story_data, featured_media_id):
    headers = get_wp_auth_header()
    headers["Content-Type"] = "application/json"

    category_name = story_data.get("category")
    category_id = CATEGORY_IDS.get(category_name)

    if category_id is None:
        category_id = CATEGORY_IDS["3-5 minutter"]

    title = story_data["title"]
    seo_title = story_data.get("seo_title", title)
    meta_description = story_data.get("meta_description", "")
    story_html = story_data["story_html"]
    slug = story_data["slug"]

    post_payload = {
        "title": title,
        "slug": slug,
        "content": story_html,
        "status": "publish",
        "categories": [category_id],
        "featured_media": featured_media_id,
        "excerpt": meta_description
    }

    url = f"{WP_URL}/wp-json/wp/v2/posts"
    resp = requests.post(url, headers=headers, data=json.dumps(post_payload))

    if resp.status_code not in (200, 201):
        raise ValueError(f"Fejl ved oprettelse af post: {resp.status_code} {resp.text}")

    return resp.json()

@app.route("/run-daily", methods=["GET"])
def run_daily():
    try:
        story_data = generate_story_with_gpt()
        img_bytes = generate_image(story_data["image_prompt"])
        media_id = upload_image_to_wordpress(img_bytes)
        post = create_post_in_wordpress(story_data, media_id)

        return jsonify({
            "status": "ok",
            "post_id": post.get("id"),
            "title": post.get("title", {}).get("rendered"),
            "link": post.get("link")
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/", methods=["GET"])
def index():
    return "AI daily story service kører."

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)

"""
One-time XML feed generator for SkyShop/Allegro.
Fetches Darymex XML feed, rewrites <name> and <desc> using Claude API,
outputs a valid XML file with identical structure for SkyShop import.
"""

import os
import sys
import time
import copy
import io
import requests
from lxml import etree
import anthropic

# Fix Windows console encoding for Polish characters
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# ─── Configuration ──────────────────────────────────────────────────────────

SOURCE_XML_URL = "https://b2b.darymex.pl/xml?id=55"
OUTPUT_FILE = os.path.join(os.path.dirname(__file__), "data", "output_feed.xml")
ORIGINAL_CACHE = os.path.join(os.path.dirname(__file__), "data", "original.xml")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = "claude-sonnet-4-5-20250929"  # Fast + capable, cost-effective for batch

# Delay between API calls to avoid rate limiting (seconds)
API_DELAY = 2

# ─── AI Prompt Template ────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an expert in creating high-converting Allegro listings.
You write in Polish. You are professional, factual, and concise.
You NEVER invent information. You use ONLY the data provided."""

def build_product_prompt(name, description, images, attrs):
    """Build the prompt for a single product transformation."""

    image_list = "\n".join(f"  {i+1}. {url}" for i, url in enumerate(images))
    attr_list = "\n".join(f"  - {k}: {v}" for k, v in attrs.items())

    return f"""Given this product data, generate an Allegro-optimized title and HTML description.

PRODUCT NAME: {name}
PRODUCT DESCRIPTION: {description}
IMAGES:
{image_list}
ATTRIBUTES:
{attr_list}

RULES FOR TITLE:
- Maximum 75 characters INCLUDING spaces
- Each word starts with a capital letter (except "i" which stays lowercase)
- No clickbait words: "promocja", "najlepszy", "premium", "hit", "gwarantuje"
- No wholesaler/manufacturer name in the title
- No "|" character
- Use relevant product keywords

RULES FOR HTML DESCRIPTION:
- Language: Polish
- Tone: Professional, factual
- No emojis
- ONLY these HTML tags: <table>, <tr>, <td>, <img>, <p>, <ul>, <li>, <b>, <h2>
- NO CSS, NO JavaScript, NO inline styles
- No clickbait words
- Use ONLY factual information from the provided data
- Do NOT invent features or specifications

STRUCTURE:
Section 1 - Top table: first image + intro paragraph + 3 main advantages as <ul>
Section 2 - Next image + <h2><b>Najwazniejsze Cechy</b></h2> + features as <ul>
Section 3 - Next image + <h2><b>Dane Techniczne</b></h2> + specs as <ul>
Section 4 - Next image + <h2><b>Informacje Dodatkowe</b></h2> + extra info as <ul>

If fewer than 4 images are available, leave <td> empty for missing images.
Use each image only once, in order.

OUTPUT FORMAT (strict):
Line 1: TITLE: <the title>
Line 2: empty
Line 3 onwards: the full HTML description (nothing else, no markdown fences)"""


# ─── XML Parsing ────────────────────────────────────────────────────────────

def fetch_xml(url):
    """Download XML feed and cache it locally."""
    print(f"Fetching XML from {url}...")
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()

    os.makedirs(os.path.dirname(ORIGINAL_CACHE), exist_ok=True)
    with open(ORIGINAL_CACHE, "wb") as f:
        f.write(resp.content)

    print(f"  Cached original XML ({len(resp.content)} bytes)")
    return resp.content


def parse_product(offer_el):
    """Extract product data from an <o> element."""
    product = {
        "id": offer_el.get("id"),
        "name": "",
        "desc": "",
        "images": [],
        "attrs": {},
    }

    # Name
    name_el = offer_el.find("name")
    if name_el is not None and name_el.text:
        product["name"] = name_el.text.strip()

    # Description (may contain HTML inside CDATA)
    desc_el = offer_el.find("desc")
    if desc_el is not None and desc_el.text:
        product["desc"] = desc_el.text.strip()

    # Images: <main url="..."/> then <i url="..."/>
    imgs_el = offer_el.find("imgs")
    if imgs_el is not None:
        main_img = imgs_el.find("main")
        if main_img is not None:
            product["images"].append(main_img.get("url"))
        for extra_img in imgs_el.findall("i"):
            product["images"].append(extra_img.get("url"))

    # Attributes: <a name="...">value</a>
    attrs_el = offer_el.find("attrs")
    if attrs_el is not None:
        for a in attrs_el.findall("a"):
            attr_name = a.get("name", "")
            attr_value = a.text.strip() if a.text else ""
            if attr_name and attr_value:
                product["attrs"][attr_name] = attr_value

    return product


# ─── AI Rewriter ────────────────────────────────────────────────────────────

def rewrite_product(client, product):
    """Call Claude API to generate new title and description."""
    prompt = build_product_prompt(
        product["name"],
        product["desc"],
        product["images"],
        product["attrs"],
    )

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )

        text = response.content[0].text.strip()

        # Parse response: first line is "TITLE: ...", rest is HTML
        lines = text.split("\n")
        title = ""
        html_start = 0

        for i, line in enumerate(lines):
            if line.startswith("TITLE:"):
                title = line[6:].strip()
                html_start = i + 1
                break

        # Skip empty lines after title
        while html_start < len(lines) and not lines[html_start].strip():
            html_start += 1

        html_desc = "\n".join(lines[html_start:]).strip()

        # Remove markdown code fences if the model added them
        if html_desc.startswith("```html"):
            html_desc = html_desc[7:]
        if html_desc.startswith("```"):
            html_desc = html_desc[3:]
        if html_desc.endswith("```"):
            html_desc = html_desc[:-3]
        html_desc = html_desc.strip()

        if not title or not html_desc:
            print(f"    WARNING: Empty AI response, keeping original")
            return None

        return {"title": title, "description": html_desc}

    except Exception as e:
        print(f"    ERROR calling Claude API: {e}")
        return None


# ─── Main Transform ─────────────────────────────────────────────────────────

def transform_feed(xml_bytes):
    """Parse XML, rewrite products, output new XML."""

    if not ANTHROPIC_API_KEY:
        print("ERROR: Set ANTHROPIC_API_KEY environment variable.")
        print("  Windows:  set ANTHROPIC_API_KEY=sk-ant-...")
        print("  Linux:    export ANTHROPIC_API_KEY=sk-ant-...")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Parse with CDATA preservation
    parser = etree.XMLParser(strip_cdata=False, recover=True)
    tree = etree.fromstring(xml_bytes, parser)

    offers = tree.findall(".//o")
    total = len(offers)
    print(f"\nFound {total} products. Processing all...\n")

    success_count = 0
    fail_count = 0

    for idx, offer in enumerate(offers):
        product = parse_product(offer)
        product_id = product["id"]
        product_name = product["name"]

        print(f"[{idx+1}/{total}] Product #{product_id}: {product_name}")

        result = rewrite_product(client, product)

        if result:
            # Replace <name> with CDATA to match original format
            name_el = offer.find("name")
            if name_el is not None:
                name_el.text = etree.CDATA(result["title"])

            # Replace <desc> with CDATA so HTML is not escaped
            desc_el = offer.find("desc")
            if desc_el is not None:
                desc_el.text = etree.CDATA(result["description"])

            print(f"    OK -> \"{result['title']}\"")
            success_count += 1
        else:
            print(f"    SKIPPED (keeping original)")
            fail_count += 1

        # Rate limit delay between API calls
        if idx < total - 1:
            time.sleep(API_DELAY)

    # ─── Write Output ───────────────────────────────────────────────────

    print(f"\n{'='*60}")
    print(f"Rewritten: {success_count}/{total}")
    print(f"Skipped:   {fail_count}/{total}")

    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)

    # Write with XML declaration and CDATA preservation
    output_tree = etree.ElementTree(tree)
    output_tree.write(
        OUTPUT_FILE,
        xml_declaration=True,
        encoding="UTF-8",
        pretty_print=True,
    )

    print(f"\nOutput saved to: {OUTPUT_FILE}")
    print("Done! Import this file into SkyShop.")


# ─── Entry Point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("  Darymex XML Feed Generator for SkyShop/Allegro")
    print("=" * 60)

    xml_bytes = fetch_xml(SOURCE_XML_URL)
    transform_feed(xml_bytes)

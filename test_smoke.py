from pathlib import Path
from bs4 import BeautifulSoup
from scraper_365scores import extract_article_text, extract_featured_image, extract_published_at

soup = BeautifulSoup(Path("tests_fixture.html").read_text(encoding="utf-8"), "html.parser")
assert "هذه فقرة تجريبية" in extract_article_text(soup)
assert extract_featured_image(soup) == "https://example.com/image.jpg"
assert extract_published_at(soup) is not None
print("smoke test passed")

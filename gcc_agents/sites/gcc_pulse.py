"""GCC Pulse news-and-updates: JS-rendered list (#news-results > .news-day > .news-item)."""
from datetime import date

from .base import Doc, in_window, parse_any_date

LISTING = "https://gcc-pulse.com/news-and-updates/"

_JS = """() => [...document.querySelectorAll('#news-results .news-day')].map(day => ({
  date: (day.querySelector('h3') || {}).innerText || '',
  items: [...day.querySelectorAll('.news-item')].map(it => ({
    title: (it.querySelector('h4') || {}).innerText || '',
    text: [...it.querySelectorAll('p:not(.meta)')].map(p => p.innerText).join(' ')
  }))
}))"""


def parse_days(days: list[dict], today: date, lookback_days: int) -> list[Doc]:
    docs: list[Doc] = []
    for day in days:
        published = parse_any_date(day.get("date", ""))
        if not in_window(published, today, lookback_days):
            continue
        for item in day.get("items", []):
            if item.get("title"):
                docs.append(
                    Doc(url=LISTING, published=published, text=f"Title: {item['title']}\n{item.get('text', '')}")
                )
    return docs


def fetch(context, settings, today: date) -> list[Doc]:
    page = context.new_page()
    page.goto(LISTING, wait_until="networkidle", timeout=45000)
    page.wait_for_selector("#news-results .news-day", timeout=15000)
    days = page.evaluate(_JS)
    page.close()
    return parse_days(days, today, settings.lookback_days)

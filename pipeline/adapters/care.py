import io
import re
import time
from datetime import date, datetime
from urllib.parse import quote, unquote

import requests

from .base import Adapter

LIST_URL = 'https://www.careratings.com/rrcompany'
PAGE_URL = 'https://www.careratings.com/find-ratings'
PDF_BASE = 'https://www.careratings.com/upload/CompanyFiles/PR/'
YEAR_ID = '2022'          # copied from the browser request; see notes
PAUSE_SECONDS = 0.5

HEADERS = {
    'Accept': 'application/json, text/javascript, */*; q=0.01',
    'Accept-Language': 'en-GB,en-US;q=0.9,en;q=0.8',
    'X-Requested-With': 'XMLHttpRequest',
    'Referer': PAGE_URL,
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36'),
}


# ------------------------------------------------------------ helpers
def _file_name(link: str) -> str:
    """Last part of a link (decoded, lower-case). Works for a full URL or a bare file name."""
    return unquote((link or '').strip().rstrip('/').split('/')[-1]).lower()


def _cr(x: float) -> str:
    return f'Rs. {x:,.2f} Cr.'


_WATCH = {'RWD': 'Rating Watch with Developing Implications',
          'RWP': 'Rating Watch with Positive Implications',
          'RWN': 'Rating Watch with Negative Implications'}


def split_rating(text: str):
    """'CARE A; Stable / CARE A1' -> ('CARE A / CARE A1', 'Stable'); '(RWD)' becomes the outlook."""
    s = re.sub(r'\s+', ' ', text or '').strip()
    outlook = None
    m = re.search(r'\((RWD|RWP|RWN)\)', s)
    if m:
        outlook = _WATCH[m.group(1)]
        s = s.replace(m.group(0), ' ')
    m = re.search(r';\s*(Stable|Positive|Negative|Developing)\b', s, re.I)
    if m:
        outlook = outlook or m.group(1).capitalize()
        s = re.sub(r';\s*(Stable|Positive|Negative|Developing)\b', '', s, flags=re.I)
    s = re.sub(r'\s+', ' ', s).strip(' ;/')
    return s, outlook


_ROW_RE = re.compile(
    r'(?P<inst>.+?)\s+'
    r'(?:(?P<reg>RBI|SEBI|MCA|IRDAI|PFRDA|NHB|IFSCA|NABARD)\s+)?'
    r'(?P<amt>\d[\d,]*\.\d+)\s+'
    r'(?P<mid>.*?)\s*'
    r'(?P<act>Provisionally Assigned|Reaffirmed and Withdrawn|Assigned|Reaffirmed|Revised|Upgraded|'
    r'Downgraded|Withdrawn|Migrated|Moved|Continued|Reinstated|Suspended)'
    r'(?:(?<=Withdrawn)\s+Withdrawn\b)?')


def _summary_box(text: str):
    flat = re.sub(r'\s+', ' ', text or '')
    end = flat.find('Details of instruments')
    if end < 0:
        return None
    start = flat.rfind('Action', 0, end)
    if start < 0:
        return None
    box = flat[start + len('Action'):end]
    return re.sub(r'^\s*(?:(?:Cash|Over|Collateral)\s+)+', '', box).strip()


_LEFTOVER_RE = re.compile(r'^(?:from\s+)?(?:Provisional\s+)?CARE\s+[A-D]{1,3}[+\-]?\d?[+\-]?(?:\s*\((?:CE|SO)\))?'
                          r'(?:;\s*(?:Stable|Positive|Negative|Developing))?(?:\s*/\s*CARE\s+\S+)?\s+(?=[A-Za-z])|^from\s+', re.I)


def parse_pr_text(text: str) -> list:
    """Rows of the 'Facilities/Instruments ... Rating Action' box on page 1 of a CARE press release.
    Returns [{'instrument','amount','amt','rating','outlook','action'}]; empty list if layout not recognised."""
    box = _summary_box(text)
    if not box:
        return []
    out = []
    for m in _ROW_RE.finditer(box):
        inst = re.sub(r'\s+', ' ', m.group('inst')).strip()
        inst = _LEFTOVER_RE.sub('', inst)          # previous rating of the row above ("from CARE A-; Stable")
        rm = re.search(r'(?:Provisional\s+)?CARE\b.*', m.group('mid') or '')
        rating, outlook = split_rating(rm.group(0)) if rm else ('', None)
        action = m.group('act')
        if not rating:
            rating = 'Withdrawn' if 'Withdrawn' in action else None
        amt = float(m.group('amt').replace(',', ''))
        out.append({'instrument': inst, 'amount': _cr(amt), 'amt': amt,
                    'rating': rating, 'outlook': outlook or 'Nil', 'action': action})
    return out


_PREV_RE = re.compile(r'\bfrom\s+((?:Provisional\s+)?CARE\s+[A-D]{1,3}[+\-]?\d?[+\-]?(?:\s*\((?:CE|SO)\))?'
                      r'(?:;\s*(?:Stable|Positive|Negative|Developing))?)')


def _distinct(items):
    seen, out = set(), []
    for x in items:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def build_report_fields(parsed: list, box: str) -> dict:
    """Collapse the facility rows of ONE report into one dashboard row (one report per row)."""
    ratings = []
    for p in parsed:
        r = p['rating']
        if r and p['outlook'] not in (None, 'Nil') and not r.startswith('Withdrawn'):
            r = f"{r}; {p['outlook']}"
        ratings.append(r)
    actions = _distinct(p['action'] for p in parsed)
    return {
        'Instrument / Facility': '; '.join(_distinct(p['instrument'] for p in parsed)),
        'Amount': _cr(sum(p['amt'] for p in parsed)),
        'Rating': '; '.join(_distinct(ratings)) or None,
        'Outlook': next((p['outlook'] for p in parsed if p['outlook'] not in (None, 'Nil')), None),
        'Rating Action': ', '.join(actions) or None,
        'Previous Rating': '; '.join(_distinct(_PREV_RE.findall(box or ''))) or None,
        'Facility Amounts (Rs crore)': '; '.join(f"{p['amt']:,.2f}".replace(',', '') for p in parsed),
        'Rating Summary': box,
    }


class CareAdapter(Adapter):
    name = 'CARE Ratings'
    enabled = True

    def key(self, row: dict) -> tuple:
        # One report (PDF) = one row.
        return (self.name, _file_name(row.get('Link')))

    # ---- network helpers -------------------------------------------------
    def _list(self, frm: date, to: date) -> list:
        params = {'companyName': '', 'YearID': YEAR_ID,
                  'fdate': frm.strftime('%m/%d/%Y'), 'tdate': to.strftime('%m/%d/%Y')}
        last = None
        for attempt in range(3):
            try:
                r = self.s.get(LIST_URL, params=params, timeout=90)
                r.raise_for_status()
                data = r.json()
                if not isinstance(data, dict) or not isinstance(data.get('data'), list):
                    raise ValueError('unexpected response shape')
                return data['data']
            except Exception as e:
                last = e
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f'CARE list request failed: {last}')

    def _pdf_text(self, file_url: str):
        """Return (text of first 2 pages, total page count); (None, None) if the file is gone."""
        from pypdf import PdfReader
        url = PDF_BASE + quote(file_url, safe="/()&+-._,~!'")
        last = None
        for attempt in range(3):
            try:
                r = self.s.get(url, timeout=90)
                if r.status_code in (404, 410):
                    return None, None
                r.raise_for_status()
                reader = PdfReader(io.BytesIO(r.content))
                text = '\n'.join((p.extract_text() or '') for p in reader.pages[:2])
                return text, len(reader.pages)
            except Exception as e:
                last = e
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f'PDF download/read failed for {file_url}: {last}')

    # ---- row building: ONE ROW PER REPORT --------------------------------
    def _rows_for(self, d: dict) -> list:
        file_url = (d.get('FileURL') or '').strip()
        company = re.sub(r'\s+', ' ', d.get('CompanyName') or '').strip()
        iso = (d.get('PublishedDate') or '')[:10]
        dt = datetime.strptime(iso, '%Y-%m-%d')
        row = {'Rating Agency': self.name, 'Date': iso, 'Company Name': company,
               'Industry / Sector': None, 'PDF File Name': None, 'Link': file_url,
               'Report Date (PDF)': dt.strftime('%B %d, %Y'), 'Report Title': company,
               'Pages': None, 'Extraction Status': 'no_table_parsed', 'Additional Details': None}
        try:
            text, pages = self._pdf_text(file_url)
            row['Pages'] = str(pages) if pages else None
            box = _summary_box(text) if text else None
            parsed = parse_pr_text(text) if text else []
        except Exception as e:
            print(f'WARNING: {file_url}: {e}')
            parsed, box = [], None
        if parsed:
            row.update(build_report_fields(parsed, box))
            row['Extraction Status'] = 'ok'
        else:
            print(f'WARNING: could not read ratings from {file_url} ({company}); added without details')
        return [row]

    # ---- main ------------------------------------------------------------
    def fetch(self, since: date, known_keys: set) -> list:
        known_files = {k[1] for k in known_keys if k and k[0] == self.name}
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        try:
            self.s.get(PAGE_URL, timeout=60)      # picks up the site's session cookies; failure is harmless
        except Exception:
            pass
        items = self._list(since, date.today())
        if not items:
            print(f'WARNING: CARE list returned no records for {since} onwards')
            return []
        out, seen = [], set()
        for d in items:
            file_url = (d.get('FileURL') or '').strip()
            iso = (d.get('PublishedDate') or '')[:10]
            if not file_url or not iso or not (d.get('CompanyName') or '').strip():
                continue
            datetime.strptime(iso, '%Y-%m-%d')               # raises if the format ever changes
            f = _file_name(file_url)
            if f in known_files or f in seen or iso < since.isoformat():
                continue
            seen.add(f)
            out.extend(self._rows_for(d))
            time.sleep(PAUSE_SECONDS)
        return out


# In case the existing __init__.py imports the stub under a different class name
CareRatingsAdapter = CareAdapter
CareRatings = CareAdapter

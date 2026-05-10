#!/usr/bin/env python3
"""
Paper Library local server: GET/PUT papers.json + static file hosting
Usage: python3 server.py [port]  (default: 8765)
"""
import glob
import html as html_mod
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

try:
    import fitz as _fitz
    _HAS_FITZ = True
except ImportError:
    _HAS_FITZ = False

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _gen_thumb(pdf_path: str, thumb_path: str) -> bool:
    """Generate a PNG thumbnail of page 1. Returns True on success."""
    os.makedirs(os.path.dirname(thumb_path), exist_ok=True)
    if _HAS_FITZ:
        try:
            doc = _fitz.open(pdf_path)
            pix = doc[0].get_pixmap(matrix=_fitz.Matrix(150 / 72, 150 / 72))
            pix.save(thumb_path)
            doc.close()
            return True
        except Exception:
            pass
    for cmd in ["/opt/homebrew/bin/pdftoppm", "pdftoppm"]:
        if os.path.isfile(cmd) or shutil.which(cmd):
            prefix = thumb_path.removesuffix("-01.png")
            try:
                subprocess.run(
                    [cmd, "-f", "1", "-l", "1", "-r", "150", "-png", pdf_path, prefix],
                    check=True, capture_output=True,
                )
                matches = sorted(glob.glob(f"{prefix}-*.png"))
                if matches:
                    if matches[0] != thumb_path:
                        os.rename(matches[0], thumb_path)
                    return True
            except (subprocess.CalledProcessError, OSError):
                pass
            break
    return False
DATA_FILE = os.path.join(BASE_DIR, "papers.json")
BACKUP_DIR = os.path.join(BASE_DIR, ".backup")
CACHE_DIR = os.path.join(BASE_DIR, ".cache")
CITATION_GRAPH_CACHE = os.path.join(CACHE_DIR, "citation_graph.json")
PAPER_GRAPH_CACHE_DIR = os.path.join(CACHE_DIR, "paper_graphs")
os.makedirs(BACKUP_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(PAPER_GRAPH_CACHE_DIR, exist_ok=True)

# ── Citation graph extraction ────────────────────────────────────────────────

_OPENALEX_SELECT = "id,doi,title,display_name,ids,referenced_works,related_works,cited_by_count,publication_year,primary_location"
_OPENALEX_REFERENCE_SELECT = "id,doi,title,display_name,cited_by_count,publication_year,primary_location,type"
_PAPER_GRAPH_VERSION = 2
_DOI_RE = re.compile(r'\b(10\.\d{4,9}/[^\s"<>]+)', re.I)


def _norm_title(value: str) -> str:
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9]+', ' ', (value or "").lower())).strip()


def _extract_doi_from_paper(paper: dict) -> str | None:
    for value in [paper.get("doi"), paper.get("url"), paper.get("note")]:
        if not value:
            continue
        m = _DOI_RE.search(str(value))
        if m:
            return m.group(1).rstrip(".,);]")
    return None


def _short_openalex_id(value: str | None) -> str | None:
    if not value:
        return None
    return str(value).rstrip("/").split("/")[-1]


def _openalex_json(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "paper-library/1.0 (local citation graph; OpenAlex)",
        },
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _resolve_openalex_work(paper: dict) -> tuple[dict | None, str]:
    """Resolve one local paper to an OpenAlex work record."""
    doi = _extract_doi_from_paper(paper)
    if doi:
        try:
            url = "https://api.openalex.org/works/doi:" + urllib.parse.quote(doi, safe="/.")
            return _openalex_json(url + "?select=" + urllib.parse.quote(_OPENALEX_SELECT)), "doi"
        except Exception:
            pass

    title = paper.get("title") or ""
    if not title:
        return None, "missing-title"

    query = urllib.parse.urlencode({
        "search": title,
        "per-page": "5",
        "select": _OPENALEX_SELECT,
    })
    data = _openalex_json("https://api.openalex.org/works?" + query)
    target = _norm_title(title)
    best = None
    best_score = 0.0
    for candidate in data.get("results", []):
        cand_title = _norm_title(candidate.get("title") or candidate.get("display_name") or "")
        if not cand_title:
            continue
        score = difflib.SequenceMatcher(None, target, cand_title).ratio()
        if score > best_score:
            best = candidate
            best_score = score
    if best and best_score >= 0.72:
        return best, f"title:{best_score:.2f}"
    return None, "not-found"


def _build_citation_graph() -> dict:
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        payload = json.load(f)
    papers = payload.get("papers", [])
    categories = {c.get("id"): c for c in payload.get("categories", [])}

    nodes = []
    unresolved = []
    openalex_to_local = {}
    refs_by_local = {}

    for paper in papers:
        node = {
            "id": paper.get("id"),
            "short": paper.get("short") or paper.get("id"),
            "title": paper.get("title") or "",
            "cat": paper.get("cat") or "",
            "category": categories.get(paper.get("cat"), {}).get("title", paper.get("cat") or ""),
            "color": categories.get(paper.get("cat"), {}).get("color", "#64748B"),
            "venue": paper.get("venue"),
            "venueTrack": paper.get("venueTrack"),
            "ccf": paper.get("ccf"),
            "arxiv": paper.get("arxiv"),
            "url": paper.get("url"),
            "resolved": False,
            "openalex": None,
            "openalexUrl": None,
            "citationCount": None,
            "publicationYear": None,
            "externalReferenceCount": 0,
            "matchMethod": None,
        }
        try:
            work, method = _resolve_openalex_work(paper)
        except Exception as e:
            work, method = None, f"error:{type(e).__name__}"

        if work:
            openalex_id = _short_openalex_id(work.get("id"))
            refs = [_short_openalex_id(x) for x in work.get("referenced_works", []) if _short_openalex_id(x)]
            node.update({
                "resolved": True,
                "openalex": openalex_id,
                "openalexUrl": work.get("id"),
                "doi": work.get("doi"),
                "citationCount": work.get("cited_by_count"),
                "publicationYear": work.get("publication_year"),
                "externalReferenceCount": len(refs),
                "matchMethod": method,
            })
            if openalex_id:
                openalex_to_local[openalex_id] = node["id"]
                refs_by_local[node["id"]] = refs
        else:
            node["matchMethod"] = method
            unresolved.append({"id": node["id"], "short": node["short"], "reason": method})

        nodes.append(node)
        time.sleep(0.08)

    edges = []
    seen_edges = set()
    for source_id, refs in refs_by_local.items():
        for ref_openalex in refs:
            target_id = openalex_to_local.get(ref_openalex)
            if not target_id or target_id == source_id:
                continue
            edge_key = (source_id, target_id)
            if edge_key in seen_edges:
                continue
            seen_edges.add(edge_key)
            edges.append({
                "source": source_id,
                "target": target_id,
                "type": "references",
                "label": "cites",
            })

    graph = {
        "ok": True,
        "source": "OpenAlex",
        "builtAt": datetime.now().isoformat(timespec="seconds"),
        "papersMtime": os.path.getmtime(DATA_FILE),
        "nodes": nodes,
        "edges": edges,
        "unresolved": unresolved,
        "stats": {
            "paperCount": len(nodes),
            "resolvedCount": sum(1 for n in nodes if n.get("resolved")),
            "unresolvedCount": len(unresolved),
            "edgeCount": len(edges),
        },
    }
    with open(CITATION_GRAPH_CACHE, "w", encoding="utf-8") as f:
        json.dump(graph, f, ensure_ascii=False, indent=2)
    return graph


def _load_citation_graph(refresh: bool = False) -> dict:
    if not refresh and os.path.exists(CITATION_GRAPH_CACHE):
        try:
            with open(CITATION_GRAPH_CACHE, "r", encoding="utf-8") as f:
                cached = json.load(f)
            if cached.get("papersMtime") == os.path.getmtime(DATA_FILE):
                return cached
        except Exception:
            pass
    return _build_citation_graph()


def _safe_cache_id(value: str) -> str:
    return re.sub(r'[^a-zA-Z0-9_.-]+', '-', str(value or "paper")).strip("-") or "paper"


def _paper_external_url(paper: dict) -> str | None:
    if paper.get("url"):
        return paper.get("url")
    if paper.get("arxiv"):
        return f"https://arxiv.org/abs/{paper.get('arxiv')}"
    if paper.get("pdf"):
        return f"references/{paper.get('pdf')}"
    return None


def _work_primary_url(work: dict) -> str | None:
    loc = work.get("primary_location") if isinstance(work.get("primary_location"), dict) else {}
    return loc.get("landing_page_url") or work.get("doi") or work.get("id")


def _work_pdf_url(work: dict) -> str | None:
    loc = work.get("primary_location") if isinstance(work.get("primary_location"), dict) else {}
    return loc.get("pdf_url")


def _work_source_name(work: dict) -> str | None:
    loc = work.get("primary_location") if isinstance(work.get("primary_location"), dict) else {}
    source = loc.get("source") if isinstance(loc.get("source"), dict) else {}
    return source.get("display_name")


def _chunked(values: list[str], size: int):
    for i in range(0, len(values), size):
        yield values[i:i + size]


def _fetch_openalex_works_by_ids(openalex_ids: list[str]) -> dict[str, dict]:
    works: dict[str, dict] = {}
    for chunk in _chunked(openalex_ids, 80):
        params = urllib.parse.urlencode({
            "filter": "ids.openalex:" + "|".join(chunk),
            "select": _OPENALEX_REFERENCE_SELECT,
            "per-page": str(max(len(chunk), 1)),
        })
        data = _openalex_json("https://api.openalex.org/works?" + params)
        for work in data.get("results", []):
            short_id = _short_openalex_id(work.get("id"))
            if short_id:
                works[short_id] = work
        time.sleep(0.08)
    return works


def _semantic_scholar_json(paper_key: str) -> dict:
    fields = ",".join([
        "title",
        "year",
        "venue",
        "url",
        "citationCount",
        "references.paperId",
        "references.title",
        "references.year",
        "references.venue",
        "references.url",
        "references.citationCount",
        "references.externalIds",
    ])
    url = "https://api.semanticscholar.org/graph/v1/paper/" + urllib.parse.quote(paper_key, safe=":") + "?fields=" + urllib.parse.quote(fields)
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "paper-library/1.0 (local citation graph; Semantic Scholar fallback)",
        },
    )
    with urllib.request.urlopen(req, timeout=12) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _semantic_scholar_reference_nodes(paper: dict, work: dict | None, identity_maps: dict) -> list[dict]:
    candidates = []
    doi = _extract_doi_from_paper(paper)
    if not doi and work and work.get("doi"):
        doi = str(work.get("doi")).replace("https://doi.org/", "")
    if doi:
        candidates.append("DOI:" + doi)
    if paper.get("arxiv"):
        candidates.append("ARXIV:" + str(paper.get("arxiv")))

    for key in candidates:
        try:
            data = _semantic_scholar_json(key)
        except Exception:
            continue
        refs = data.get("references") or []
        nodes = []
        for ref in refs:
            if not ref or not ref.get("title"):
                continue
            external_ids = ref.get("externalIds") or {}
            doi_value = external_ids.get("DOI")
            openalex_value = _short_openalex_id(external_ids.get("OpenAlex"))
            title_key = _norm_title(ref.get("title") or "")
            local = None
            if openalex_value:
                local = identity_maps["openalex"].get(openalex_value)
            if not local and doi_value:
                local = identity_maps["doi"].get(str(doi_value).lower())
            if not local and title_key:
                local = identity_maps["title"].get(title_key)

            if local:
                node = _local_paper_node(local, identity_maps["categories"])
                node.update({
                    "kind": "local",
                    "openalex": openalex_value,
                    "openalexUrl": f"https://openalex.org/{openalex_value}" if openalex_value else None,
                    "doi": f"https://doi.org/{doi_value}" if doi_value else node.get("doi"),
                    "citationCount": ref.get("citationCount"),
                    "publicationYear": ref.get("year"),
                    "sourceName": ref.get("venue"),
                    "url": node.get("url") or ref.get("url"),
                })
            else:
                fallback_id = openalex_value or ref.get("paperId") or str(abs(hash(ref.get("title"))))
                node = {
                    "id": f"ref-{fallback_id}",
                    "kind": "external",
                    "local": False,
                    "paperId": None,
                    "short": str(ref.get("year") or "") or "ref",
                    "title": ref.get("title"),
                    "category": "外部参考文献",
                    "color": "#6A9BBF",
                    "venue": None,
                    "ccf": None,
                    "url": ref.get("url") or (f"https://doi.org/{doi_value}" if doi_value else None),
                    "arxiv": external_ids.get("ArXiv"),
                    "openalex": openalex_value,
                    "openalexUrl": f"https://openalex.org/{openalex_value}" if openalex_value else None,
                    "doi": f"https://doi.org/{doi_value}" if doi_value else None,
                    "citationCount": ref.get("citationCount"),
                    "publicationYear": ref.get("year"),
                    "sourceName": ref.get("venue"),
                    "semanticScholarId": ref.get("paperId"),
                }
            nodes.append(node)
        if nodes:
            return nodes
    return []


def _build_local_identity_maps(papers: list[dict], categories: dict) -> dict:
    doi_map = {}
    title_map = {}
    openalex_map = {}

    for paper in papers:
        doi = _extract_doi_from_paper(paper)
        if doi:
            doi_map[doi.lower()] = paper
        title_key = _norm_title(paper.get("title") or "")
        if title_key:
            title_map[title_key] = paper

    try:
        if os.path.exists(CITATION_GRAPH_CACHE):
            with open(CITATION_GRAPH_CACHE, "r", encoding="utf-8") as f:
                graph = json.load(f)
            by_id = {p.get("id"): p for p in papers}
            for node in graph.get("nodes", []):
                local = by_id.get(node.get("id"))
                openalex = _short_openalex_id(node.get("openalex") or node.get("openalexUrl"))
                if local and openalex:
                    openalex_map[openalex] = local
    except Exception:
        pass

    return {
        "doi": doi_map,
        "title": title_map,
        "openalex": openalex_map,
        "categories": categories,
    }


def _local_paper_node(paper: dict, categories: dict, *, is_center: bool = False, work: dict | None = None) -> dict:
    cat = categories.get(paper.get("cat"), {})
    openalex_id = _short_openalex_id(work.get("id")) if work else None
    return {
        "id": paper.get("id"),
        "kind": "center" if is_center else "local",
        "local": True,
        "paperId": paper.get("id"),
        "short": paper.get("short") or paper.get("id"),
        "title": paper.get("title") or "",
        "category": cat.get("title", paper.get("cat") or ""),
        "color": cat.get("color", "#64748B"),
        "venue": paper.get("venue"),
        "ccf": paper.get("ccf"),
        "url": _paper_external_url(paper),
        "arxiv": paper.get("arxiv"),
        "openalex": openalex_id,
        "openalexUrl": work.get("id") if work else None,
        "doi": work.get("doi") if work else _extract_doi_from_paper(paper),
        "citationCount": work.get("cited_by_count") if work else None,
        "publicationYear": work.get("publication_year") if work else None,
        "sourceName": _work_source_name(work) if work else paper.get("venue"),
        "pdfUrl": _work_pdf_url(work) if work else None,
    }


def _reference_node_from_work(work: dict, identity_maps: dict) -> dict:
    openalex_id = _short_openalex_id(work.get("id"))
    doi = str(work.get("doi") or "").replace("https://doi.org/", "").lower()
    title_key = _norm_title(work.get("title") or work.get("display_name") or "")
    local = None
    if openalex_id:
        local = identity_maps["openalex"].get(openalex_id)
    if not local and doi:
        local = identity_maps["doi"].get(doi)
    if not local and title_key:
        local = identity_maps["title"].get(title_key)

    if local:
        node = _local_paper_node(local, identity_maps["categories"], work=work)
        node["openalex"] = openalex_id
        node["openalexUrl"] = work.get("id")
        node["doi"] = work.get("doi")
        node["citationCount"] = work.get("cited_by_count")
        node["publicationYear"] = work.get("publication_year")
        node["sourceName"] = _work_source_name(work)
        node["pdfUrl"] = _work_pdf_url(work)
        return node

    return {
        "id": f"ref-{openalex_id}",
        "kind": "external",
        "local": False,
        "paperId": None,
        "short": str(work.get("publication_year") or "") or openalex_id,
        "title": work.get("title") or work.get("display_name") or openalex_id,
        "category": "外部参考文献",
        "color": "#6A9BBF",
        "venue": None,
        "ccf": None,
        "url": _work_primary_url(work),
        "arxiv": None,
        "openalex": openalex_id,
        "openalexUrl": work.get("id"),
        "doi": work.get("doi"),
        "citationCount": work.get("cited_by_count"),
        "publicationYear": work.get("publication_year"),
        "sourceName": _work_source_name(work),
        "pdfUrl": _work_pdf_url(work),
        "workType": work.get("type"),
    }


def _build_paper_reference_graph(paper_id: str) -> dict:
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        payload = json.load(f)
    papers = payload.get("papers", [])
    categories = {c.get("id"): c for c in payload.get("categories", [])}
    paper = next((p for p in papers if p.get("id") == paper_id), None)
    if not paper:
        return {"ok": False, "error": f"Paper not found: {paper_id}", "paperId": paper_id}

    try:
        center_work, method = _resolve_openalex_work(paper)
    except Exception as e:
        center_work, method = None, f"error:{type(e).__name__}"

    center = _local_paper_node(paper, categories, is_center=True, work=center_work)
    center["matchMethod"] = method
    center["resolved"] = bool(center_work)

    if not center_work:
        return {
            "ok": False,
            "source": "OpenAlex",
            "version": _PAPER_GRAPH_VERSION,
            "paperId": paper_id,
            "builtAt": datetime.now().isoformat(timespec="seconds"),
            "papersMtime": os.path.getmtime(DATA_FILE),
            "error": f"OpenAlex 未匹配：{method}",
            "center": center,
            "nodes": [center],
            "edges": [],
            "references": [],
            "stats": {
                "referenceCount": 0,
                "localReferenceCount": 0,
                "externalReferenceCount": 0,
            },
        }

    ref_ids = []
    seen = set()
    for ref in center_work.get("referenced_works", []) or []:
        ref_id = _short_openalex_id(ref)
        if ref_id and ref_id not in seen:
            seen.add(ref_id)
            ref_ids.append(ref_id)

    identity_maps = _build_local_identity_maps(papers, categories)
    works_by_id = _fetch_openalex_works_by_ids(ref_ids) if ref_ids else {}
    references = []
    nodes = [center]
    edges = []
    local_ref_count = 0

    if ref_ids:
        for ref_id in ref_ids:
            work = works_by_id.get(ref_id)
            if work:
                node = _reference_node_from_work(work, identity_maps)
            else:
                node = {
                    "id": f"ref-{ref_id}",
                    "kind": "external",
                    "local": False,
                    "paperId": None,
                    "short": ref_id,
                    "title": ref_id,
                    "category": "外部参考文献",
                    "color": "#6A9BBF",
                    "url": f"https://openalex.org/{ref_id}",
                    "openalex": ref_id,
                    "openalexUrl": f"https://openalex.org/{ref_id}",
                    "citationCount": None,
                    "publicationYear": None,
                    "sourceName": None,
                }
            references.append(node)
    else:
        references = _semantic_scholar_reference_nodes(paper, center_work, identity_maps)

    seen_node_ids = set()
    for node in references:
        node_id = node.get("id")
        if not node_id or node_id in seen_node_ids:
            continue
        seen_node_ids.add(node_id)
        if node.get("local"):
            local_ref_count += 1
        nodes.append(node)
        edges.append({
            "source": center["id"],
            "target": node_id,
            "type": "references",
            "label": "引用",
            "relation": "当前论文引用该论文",
            "scope": "local" if node.get("local") else "external",
        })

    graph = {
        "ok": True,
        "source": "OpenAlex" if ref_ids or not references else "Semantic Scholar fallback",
        "version": _PAPER_GRAPH_VERSION,
        "paperId": paper_id,
        "builtAt": datetime.now().isoformat(timespec="seconds"),
        "papersMtime": os.path.getmtime(DATA_FILE),
        "center": center,
        "nodes": nodes,
        "edges": edges,
        "references": references,
        "stats": {
            "referenceCount": len(references),
            "localReferenceCount": local_ref_count,
            "externalReferenceCount": len(references) - local_ref_count,
        },
    }
    return graph


def _paper_graph_cache_file(paper_id: str) -> str:
    return os.path.join(PAPER_GRAPH_CACHE_DIR, _safe_cache_id(paper_id) + ".json")


def _load_paper_reference_graph(paper_id: str, refresh: bool = False) -> dict:
    cache_file = _paper_graph_cache_file(paper_id)
    if not refresh and os.path.exists(cache_file):
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                cached = json.load(f)
            if cached.get("papersMtime") == os.path.getmtime(DATA_FILE) and cached.get("version") == _PAPER_GRAPH_VERSION:
                return cached
        except Exception:
            pass

    graph = _build_paper_reference_graph(paper_id)
    if graph.get("center"):
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(graph, f, ensure_ascii=False, indent=2)
    return graph

# ── PDF metadata extraction ──────────────────────────────────────────────────

_INST_KW = re.compile(
    r'\b('
    # English generic
    r'university|universit[eéy]|institute|institution|college|school of|faculty|'
    r'department|dept\.?|division|center|centre|laboratory|laboratories|lab\b|'
    r'research|technology|national|international|'
    # Well-known US
    r'carnegie|mellon|stanford|berkeley|harvard|princeton|caltech|cornell|columbia|'
    r'yale|nyu|ucsd|uiuc|gatech|mit\b|'
    # Well-known non-US
    r'oxford|cambridge|imperial|ucl|edinburgh|eth\b|epfl|inria|mpi\b|max.?planck|'
    r'tsinghua|peking|fudan|zhejiang|sjtu|tongji|hkust|nus\b|ntu\b|kaist|kaust|'
    r'toronto|mcgill|waterloo|amsterdam|delft|lund|kth\b|aalto|rwth|'
    # Industry / AI labs
    r'deepmind|openai|anthropic|google|microsoft|meta\b|nvidia|apple|amazon|'
    r'physical intelligence|physical.intelligence|'
    r'corporation|corp\b|inc\b|ltd\b|gmbh'
    r')\b',
    re.I,
)

_DEPT_PAT = re.compile(
    r'\b(department|dept\.?|school|faculty|division|center|centre|lab|laboratory|'
    r'institute|college)\s+(of|for)\b',
    re.I,
)

_SUPERSCRIPT_CLEAN = re.compile(r'^[\d¹²³⁴⁵⁶⁷⁸⁹⁰,\s\*†‡§¶]+')
_LOCATION_SUFFIX = re.compile(
    r',\s*(USA|U\.S\.A\.|UK|U\.K\.|China|Japan|Germany|France|Canada|'
    r'Australia|Singapore|South Korea|Switzerland|Netherlands|Sweden|'
    r'Italy|Spain|Brazil|India|Israel|Denmark|Norway|Finland)\s*$',
    re.I,
)


def _extract_pdf_meta(pdf_path: str):
    """Return (title, arxiv_id, org) extracted from first-page text of pdf_path."""
    pdf_title = None
    pdf_arxiv = None
    pdf_org = None
    try:
        txt = subprocess.run(
            ["pdftotext", "-f", "1", "-l", "2", pdf_path, "-"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        lines = [ln.strip() for ln in txt.split("\n") if ln.strip()]

        # ── Title block: collect up to 2 long lines before abstract/section markers ──
        title_parts = []
        stop_idx = min(15, len(lines))
        for i, line in enumerate(lines[:15]):
            if re.search(
                r'^(abstract|introduction|\d[\.\s]|©|arxiv:|submitted|preprint|accepted|keywords|email)',
                line.lower(),
            ):
                stop_idx = i
                break
            if len(line) > 12:
                title_parts.append(line)
            if len(title_parts) >= 2:
                stop_idx = i + 1
                break

        if title_parts:
            pdf_title = " ".join(title_parts)

        # ── arXiv ID ──
        arxiv_m = re.search(r'arXiv[:\s]*(\d{4}\.\d{4,5})', txt, re.I)
        if not arxiv_m:
            arxiv_m = re.search(r'\b(\d{4}\.\d{4,5})\b', txt)
        if arxiv_m:
            pdf_arxiv = arxiv_m.group(1)

        # ── Org: search lines after title block (up to 25 lines) ──
        search_lines = lines[stop_idx: stop_idx + 25]

        candidates = []
        for line in search_lines:
            # Skip very short, very long, URLs, or obvious non-affiliation lines
            if len(line) < 5 or len(line) > 200:
                continue
            if re.search(r'^https?://', line):
                continue
            if re.search(r'^(abstract|keywords|email|correspondence|equal\s+contribution)', line.lower()):
                break

            cleaned = _SUPERSCRIPT_CLEAN.sub('', line).strip(' ,;*†‡§¶')
            if not cleaned or re.search(r'^https?://', cleaned):
                continue

            score = 0
            if _DEPT_PAT.search(cleaned):
                score += 3       # "Department of X" is very reliable
            if _INST_KW.search(cleaned):
                score += 2
            if _LOCATION_SUFFIX.search(cleaned):
                score += 1       # ends with a country name
            # Lines with @ are emails — don't use the raw line as org
            if '@' in cleaned:
                continue

            if score > 0:
                candidates.append((score, cleaned))

        if candidates:
            # Pick highest-scoring; prefer shorter lines when tied (avoid multi-author lines)
            candidates.sort(key=lambda x: (-x[0], len(x[1])))
            best = candidates[0][1]
            # If line lists multiple institutions (e.g. "Inst A, 2 Inst B" or "Inst A 2 Inst B"), keep only the first
            first_inst = re.split(r'(?:,\s*|\s+)\d+\s+[A-Z]', best)[0].strip(' ,')
            pdf_org = first_inst if first_inst else best

        # Fallback: academic email domain → institution hint
        if not pdf_org:
            for em in re.finditer(r'@([\w\-\.]+\.(edu|ac\.\w{2,}))', txt, re.I):
                domain = em.group(1).lower()
                if 'github' not in domain and 'arxiv' not in domain:
                    parts = domain.split('.')
                    pdf_org = '.'.join(parts[-3:]) if len(parts) >= 3 else domain
                    break

    except Exception:
        pass
    return pdf_title, pdf_arxiv, pdf_org


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=BASE_DIR, **kwargs)

    def _send_json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/citation-graph":
            try:
                qs = urllib.parse.parse_qs(parsed.query)
                refresh = qs.get("refresh", ["0"])[0] in ("1", "true", "yes")
                graph = _load_citation_graph(refresh=refresh)
                return self._send_json(graph)
            except Exception as e:
                if os.path.exists(CITATION_GRAPH_CACHE):
                    try:
                        with open(CITATION_GRAPH_CACHE, "r", encoding="utf-8") as f:
                            graph = json.load(f)
                        graph["ok"] = False
                        graph["warning"] = f"Refresh failed, serving cached graph: {e}"
                        return self._send_json(graph)
                    except Exception:
                        pass
                return self._send_json({"ok": False, "error": str(e)}, 500)
        if parsed.path == "/api/paper-graph":
            try:
                qs = urllib.parse.parse_qs(parsed.query)
                paper_id = qs.get("id", [""])[0]
                refresh = qs.get("refresh", ["0"])[0] in ("1", "true", "yes")
                if not paper_id:
                    return self._send_json({"ok": False, "error": "Missing paper id"}, 400)
                graph = _load_paper_reference_graph(paper_id, refresh=refresh)
                return self._send_json(graph)
            except Exception as e:
                return self._send_json({"ok": False, "error": str(e)}, 500)
        if self.path == "/api/papers":
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                payload = json.load(f)
            mtime = os.path.getmtime(DATA_FILE)
            data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Expose-Headers", "X-Mtime")
            self.send_header("X-Mtime", f"{mtime:.6f}")
            self.end_headers()
            self.wfile.write(data)
            return
        super().do_GET()

    def do_PUT(self):
        if self.path == "/api/papers":
            try:
                payload = self._read_json_body()
                if "categories" not in payload or "papers" not in payload:
                    return self._send_json({"error": "missing keys"}, 400)

                # Optimistic concurrency: reject stale writes
                client_mtime = self.headers.get("X-If-Mtime")
                current_mtime = os.path.getmtime(DATA_FILE)
                if client_mtime is not None:
                    try:
                        if float(client_mtime) + 0.001 < current_mtime:
                            return self._send_json({
                                "error": "stale",
                                "message": "File was modified by another client. Please refresh and retry.",
                                "server_mtime": current_mtime,
                                "client_mtime": float(client_mtime),
                            }, 409)
                    except ValueError:
                        pass

                # Backup before overwrite
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                shutil.copy2(DATA_FILE, os.path.join(BACKUP_DIR, f"papers_{ts}.json"))

                with open(DATA_FILE, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)

                new_mtime = os.path.getmtime(DATA_FILE)
                data = json.dumps({"ok": True, "count": len(payload["papers"]), "mtime": new_mtime}, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Expose-Headers", "X-Mtime")
                self.send_header("X-Mtime", f"{new_mtime:.6f}")
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return
        self.send_response(405)
        self.end_headers()

    def do_POST(self):
        if self.path == "/api/upload":
            try:
                length = int(self.headers.get("Content-Length", 0))
                if not length:
                    return self._send_json({"error": "empty body"}, 400)
                raw_name = self.headers.get("X-Filename", "upload.pdf")
                filename = os.path.basename(urllib.parse.unquote(raw_name))
                if not filename.lower().endswith(".pdf"):
                    filename += ".pdf"
                pdf_path = os.path.join(BASE_DIR, "references", filename)
                with open(pdf_path, "wb") as f:
                    f.write(self.rfile.read(length))
                thumb_name = filename[:-4] + "-01.png"
                thumb_path = os.path.join(BASE_DIR, "references", "thumbs", thumb_name)
                thumb_ok = _gen_thumb(pdf_path, thumb_path)
                # arXiv ID from filename
                pdf_arxiv = None
                stem_name = os.path.splitext(filename)[0]
                m = re.search(r'\b(\d{4}\.\d{4,5})\b', stem_name)
                if m:
                    pdf_arxiv = m.group(1)
                # Title from pdfinfo metadata (highest quality)
                pdf_title = None
                try:
                    info_out = subprocess.run(
                        ["pdfinfo", pdf_path], capture_output=True, text=True, timeout=10
                    ).stdout
                    for line in info_out.split("\n"):
                        if line.lower().startswith("title:"):
                            t = line[6:].strip()
                            if t and t.lower() not in ("untitled", "unknown", ""):
                                pdf_title = t
                            break
                except Exception:
                    pass
                # Title, arXiv ID, org from first-page text
                txt_title, txt_arxiv, pdf_org = _extract_pdf_meta(pdf_path)
                if not pdf_title:
                    pdf_title = txt_title
                if not pdf_arxiv:
                    pdf_arxiv = txt_arxiv
                self._send_json({"ok": True, "pdf": filename, "thumb": thumb_name if thumb_ok else None,
                                 "title": pdf_title, "arxiv": pdf_arxiv, "org": pdf_org})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return
        if self.path == "/api/arxiv":
            try:
                body = self._read_json_body()
                raw = body.get("id", "").strip()
                m = re.search(r'\b(\d{4}\.\d{4,5}(?:v\d+)?)\b', raw)
                if not m:
                    return self._send_json({"error": "no arXiv ID found"}, 400)
                arxiv_id = m.group(1).split("v")[0]  # strip version suffix

                # Fetch metadata from arXiv Atom API
                meta_url = f"https://export.arxiv.org/api/query?id_list={arxiv_id}"
                with urllib.request.urlopen(meta_url, timeout=15) as resp:
                    xml_bytes = resp.read()
                ns = {
                    "atom": "http://www.w3.org/2005/Atom",
                    "arxiv": "http://arxiv.org/schemas/atom",
                }
                root = ET.fromstring(xml_bytes)
                entry = root.find("atom:entry", ns)
                if entry is None:
                    return self._send_json({"error": "arXiv ID not found"}, 404)
                title = (entry.findtext("atom:title", "", ns) or "").strip().replace("\n", " ")
                authors = [
                    a.findtext("atom:name", "", ns).strip()
                    for a in entry.findall("atom:author", ns)
                ]
                published = entry.findtext("atom:published", "", ns)
                year = published[:4] if published else ""
                # Affiliation from API (rarely populated; we'll also extract from PDF below)
                api_org = None
                for author in entry.findall("atom:author", ns):
                    aff = author.findtext("arxiv:affiliation", "", ns).strip()
                    if aff:
                        api_org = aff
                        break

                # Download PDF
                pdf_filename = f"{arxiv_id}.pdf"
                pdf_path = os.path.join(BASE_DIR, "references", pdf_filename)
                pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"
                req = urllib.request.Request(pdf_url, headers={"User-Agent": "paper-library/1.0"})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    pdf_bytes = resp.read()
                os.makedirs(os.path.join(BASE_DIR, "references"), exist_ok=True)
                with open(pdf_path, "wb") as f:
                    f.write(pdf_bytes)

                # Generate thumbnail
                thumb_name = f"{arxiv_id}-01.png"
                thumb_path = os.path.join(BASE_DIR, "references", "thumbs", thumb_name)
                thumb_ok = _gen_thumb(pdf_path, thumb_path)

                # Extract org from PDF text (more reliable than API affiliation field)
                _, _, pdf_org = _extract_pdf_meta(pdf_path)
                org = api_org or pdf_org

                self._send_json({
                    "ok": True,
                    "arxiv": arxiv_id,
                    "title": title,
                    "authors": authors,
                    "year": year,
                    "org": org,
                    "pdf": pdf_filename,
                    "thumb": thumb_name if thumb_ok else None,
                })
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return
        if self.path == "/api/fetch-og":
            try:
                body = self._read_json_body()
                url = body.get("url", "").strip()
                if not url:
                    return self._send_json({"error": "no url"}, 400)
                if not url.startswith("http"):
                    url = "https://" + url
                req = urllib.request.Request(
                    url, headers={"User-Agent": "Mozilla/5.0 (compatible; paper-library/1.0)"}
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    html_bytes = resp.read(512 * 1024)
                html_text = html_bytes.decode("utf-8", errors="replace")
                # Find og:image or twitter:image
                img_url = None
                for pat in [
                    r'property=["\']og:image["\'][^>]*content=["\'](https?://[^"\'>\s]+)',
                    r'content=["\'](https?://[^"\'>\s]+)["\'][^>]*property=["\']og:image["\']',
                    r'name=["\']twitter:image["\'][^>]*content=["\'](https?://[^"\'>\s]+)',
                    r'content=["\'](https?://[^"\'>\s]+)["\'][^>]*name=["\']twitter:image["\']',
                ]:
                    m = re.search(pat, html_text, re.I)
                    if m:
                        img_url = m.group(1)
                        break
                # Extract og:title and og:site_name regardless of image
                og_title = None
                og_site = None
                for key, pats in [
                    ("title", [r'property=["\']og:title["\'][^>]*content=["\'](.*?)["\']',
                               r'content=["\'](.*?)["\'][^>]*property=["\']og:title["\']']),
                    ("site",  [r'property=["\']og:site_name["\'][^>]*content=["\'](.*?)["\']',
                               r'content=["\'](.*?)["\'][^>]*property=["\']og:site_name["\']']),
                ]:
                    for pat in pats:
                        mm = re.search(pat, html_text, re.I)
                        if mm:
                            val = html_mod.unescape(mm.group(1).strip())
                            if key == "title":
                                og_title = val
                            else:
                                og_site = val
                            break

                if not img_url:
                    return self._send_json({"ok": False, "error": "页面中未找到 og:image",
                                            "title": og_title, "org": og_site})
                img_req = urllib.request.Request(
                    img_url, headers={"User-Agent": "Mozilla/5.0 (compatible; paper-library/1.0)"}
                )
                with urllib.request.urlopen(img_req, timeout=15) as resp:
                    img_bytes = resp.read(10 * 1024 * 1024)
                    content_type = resp.headers.get("content-type", "")
                ext = ".jpg" if "jpeg" in content_type or "jpg" in content_type else ".webp" if "webp" in content_type else ".png"
                parsed_host = re.sub(r'[^a-zA-Z0-9]', '_', urllib.parse.urlparse(url).netloc.replace("www.", ""))[:24]
                thumb_name = f"{parsed_host}_og{ext}"
                thumb_path = os.path.join(BASE_DIR, "references", "thumbs", thumb_name)
                os.makedirs(os.path.dirname(thumb_path), exist_ok=True)
                with open(thumb_path, "wb") as f:
                    f.write(img_bytes)
                self._send_json({"ok": True, "thumb": thumb_name, "title": og_title, "org": og_site})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)})
            return
        self.send_response(405)
        self.end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, PUT, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-If-Mtime, X-Filename")
        self.end_headers()

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    print(f"Paper Library server: http://localhost:{port}")
    print(f"Data file: {DATA_FILE}")
    print(f"Backup dir: {BACKUP_DIR}")
    ThreadingHTTPServer(("", port), Handler).serve_forever()

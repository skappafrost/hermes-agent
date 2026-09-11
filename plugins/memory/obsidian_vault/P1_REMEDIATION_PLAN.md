# P1 REMEDIATION PLAN — Query Understanding & Expansion (Phase 3)

**Created:** 2026-08-09  
**Scope:** P1 — Query Understanding & Expansion (Phase 3 of SEARCH_UPGRADE_PLAN.md)  
**Prerequisite:** P0 Complete ✅

---

## 1. CURRENT STATE ANALYSIS

### Already Implemented (from P0 work)
| Feature | Status | Location |
|---------|--------|----------|
| QueryExpander (synonyms + entity extraction) | ✅ **COMPLETE** | `embeddings.py:615` |
| SpellCorrector (Levenshtein distance) | ✅ **COMPLETE** | `embeddings.py:666` |
| MMRDiversity | ✅ **COMPLETE** | `embeddings.py:931` |
| ExponentialFreshnessDecay | ✅ **COMPLETE** | `embeddings.py:994` |
| ResultClustering | ✅ **COMPLETE** | `embeddings.py:1026` |
| HybridSearcher integration | ✅ **COMPLETE** | `embeddings.py:1098` |

### What's Missing from SEARCH_UPGRADE_PLAN.md Phase 3

| Feature | Plan Spec | Current Status | Gap |
|---------|-----------|----------------|-----|
| Query Expansion - Synonyms via Word2Vec/FastText | "Synonym expansion via word embeddings" | Hardcoded 8 synonym groups only | ❌ Missing word embedding-based expansion |
| Query Expansion - Query rewriting | "Query rewriting for common patterns" | Not implemented | ❌ Missing |
| Entity extraction integration in search | "Entity extraction (dates, people, projects)" | Regex patterns exist but not integrated in search | ❌ Not integrated |
| Spell Correction - "Did you mean?" suggestions | "Did you mean? suggestions" | `SpellCorrector.correct_query()` exists but not exposed in API | ❌ Not exposed |
| Query rewriting for common patterns | "Query rewriting for common patterns" | Not implemented | ❌ Missing |

---

## 2. P1 REMEDIATION PLAN

### P1-1: "Did you mean?" Spell Correction Suggestions (SHOULD FIX)
- **Plan ref:** Phase 3, "Did you mean? suggestions"
- **Current:** `SpellCorrector.correct_query()` exists but not exposed in search API
- **Fix:** Expose suggestions in search response when query is corrected
- **Files:** `__init__.py:_handle_search()`, `embeddings.py:SpellCorrector`

### P1-2: Query Expansion with Word Embeddings (SHOULD FIX)
- **Plan ref:** "Synonym expansion via word embeddings (Word2Vec/FastText)"
- **Current:** Hardcoded 8 synonym groups only
- **Fix:** Add optional Word2Vec/FastText model for synonym expansion
- **Files:** `embeddings.py:QueryExpander`, add optional dependency

### P1-3: Query Rewriting for Common Patterns (SHOULD FIX)
- **Plan ref:** "Query rewriting for common patterns"
- **Current:** Not implemented
- **Fix:** Add query pattern recognition and rewriting
- **Files:** `embeddings.py:QueryExpander`, add rewrite rules

### P1-4: Entity Extraction Integration in Search (SHOULD FIX)
- **Plan ref:** "Entity extraction (dates, people, projects)"
- **Current:** Regex patterns exist but not used in search
- **Fix:** Integrate extracted entities as search filters
- **Files:** `embeddings.py:QueryExpander`, `HybridSearcher.search()`

### P1-5: "Did you mean?" Suggestions in Search API (SHOULD FIX)
- **Plan ref:** "Did you mean? suggestions"
- **Current:** `SpellCorrector.correct_query()` exists but not exposed
- **Fix:** Return suggestions in search response when query is corrected
- **Files:** `__init__.py:_handle_search()`, `embeddings.py:SpellCorrector`

---

## 3. IMPLEMENTATION ORDER

| Priority | Task | Effort | Dependencies |
|----------|------|--------|--------------|
| P1-1 | "Did you mean?" suggestions in search API | Low | SpellCorrector exists |
| P1-5 | Expose spell correction suggestions in search API | Low | P1-1 |
| P1-2 | Word embedding synonym expansion | Medium | Word2Vec/FastText model |
| P1-3 | Query rewriting for common patterns | Medium | QueryExpander |
| P1-4 | Entity extraction integration in search | Medium | QueryExpander entities |

---

## 4. VERIFICATION CRITERIA

| Test | Description |
|------|-------------|
| T1 | Typo query returns "Did you mean?" suggestion |
| T2 | Query expansion finds synonyms via word embeddings |
| T3 | Common query patterns are rewritten |
| T4 | Extracted entities (dates, etc.) work as filters |
| T3 | All existing tests still pass |

---

## 5. SCOPE BOUNDARY

**DO NOT IMPLEMENT:**
- P2 items (performance warnings, clustering optimization)
- P3 items (dead code cleanup, stale imports)
- P4 items (optional enhancements)
- New Phase 5+ features

**Files to modify:**
- `embeddings.py` - QueryExpander, SpellCorrector, HybridSearcher
- `__init__.py` - _handle_search() to expose suggestions
- Optional: Add Word2Vec/FastText model download

**Tests to add:**
- Spell correction suggestions in search response
- Query expansion with word embeddings
- Entity extraction in search filters

---

*Ready to execute P1 remediation. Starting with P1-1 and P1-5 (easiest wins), then P1-2, P1-3, P1-4.*
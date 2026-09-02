# Search Algorithm Upgrade Plan for Obsidian Vault

## Current State Analysis

**What We Have:**
- BM25 (Okapi) with basic TF-IDF scoring
- SQLite FTS5 for full-text search
- Character n-gram embeddings (128-dim) for semantic similarity
- Basic phrase matching (exact substring)
- Tag/category boosts
- Date range filtering
- Field-specific search (title:, tag:, category:)
- Wildcard prefix matching
- BM25 with k1=1.2, b=0.75 (standard values)

**What Modern Search Engines Use (Missing):**

| Feature | Current | Modern Standard |
|---------|---------|-----------------|
| Semantic Embeddings | Char n-grams (weak) | Dense vectors (BERT/sentence-transformers) |
| Hybrid Search | FTS5 only | BM25 + Dense Vector (Hybrid) |
| Reranking | None | Cross-encoder reranking |
| Query Expansion | None | Synonyms, embeddings, LLM |
| Result Diversity | None | MMR / Diversity ranking |
| Freshness Decay | Linear in BM25 | Exponential decay / Recency boost |
| Query Understanding | Basic parser | Intent detection, entity extraction |
| Result Diversity | None | MMR / Clustering |
| Query Expansion | Wildcard only | Synonyms, embeddings, LLM |
| Spell Correction | None | Fuzzy matching / Levenshtein |
| Result Clustering | None | Topic clustering |
| Personalization | None | Session/user context |

---

## Upgrade Plan (Prioritized by Impact)

### Phase 1: Hybrid Search (High Impact, Medium Effort)
**Goal: Combine FTS5 BM25 with Dense Vector Search**

1. **Upgrade Embeddings**
   - Replace character n-grams with sentence-transformers (e.g., `all-MiniLM-L6-v2`, 384-dim)
   - Use ONNX Runtime for fast inference
   - Store dense vectors in SQLite (BLOB) or separate FAISS index

2. **Hybrid Retrieval**
   - Retrieve top-k from FTS5 (BM25) + top-k from vector index (ANN)
   - Merge with Reciprocal Rank Fusion (RRF) or weighted combination
   - Configurable weights: `score = α * BM25_norm + (1-α) * cos_sim`

2. **FAISS/HNSW Index** (for >10k notes)
   - Use FAISS HNSW for ANN search
   - Or SQLite-vec extension for pure SQLite

### Phase 2: Cross-Encoder Reranking (High Impact, Low Effort)
**Goal: Improve precision@k with cross-attention**

1. **Add Cross-Encoder Reranker**
   - Use `cross-encoder/ms-marco-MiniLM-L-6-v2` (fast, accurate)
   - Rerank top-50 → top-10
   - Cache cross-encoder results

### Phase 3: Query Understanding & Expansion (Medium Impact)

1. **Query Expansion**
   - Synonym expansion via word embeddings (Word2Vec/FastText)
   - Query rewriting for common patterns
   - Entity extraction (dates, people, projects)

2. **Spell Correction / Fuzzy Matching**
   - Levenshtein distance for typo tolerance
   - "Did you mean?" suggestions

### Phase 4: Result Quality (Medium Impact)

1. **Diversity / MMR (Maximal Marginal Relevance)**
   - Penalize redundant results
   - `score = λ * relevance - (1-λ) * max_sim_to_selected`

2. **Better Freshness/Recency**
   - Exponential decay: `exp(-λ * days_old)`
   - Configurable half-life

2. **Result Clustering/Grouping**
   - Cluster by topic/theme
   - Show representative per cluster

---

## Implementation Priority

| Phase | Feature | Effort | Impact | Dependencies |
|-------|---------|--------|--------|--------------|
| 1 | Dense embeddings (sentence-transformers) | Medium | ⭐⭐⭐⭐⭐ | ONNX Runtime |
| 1 | Hybrid retrieval (RRF) | Medium | ⭐⭐⭐⭐ | FAISS/sqlite-vec |
| 2 | Cross-encoder reranker | Low | ⭐⭐⭐⭐ | ONNX Runtime |
| 3 | Query expansion | Medium | ⭐⭐⭐ | Word embeddings |
| 3 | Spell correction | Low | ⭐⭐ | Levenshtein |
| 4 | MMR Diversity | Low | ⭐⭐⭐ | - |
| 4 | Exponential freshness decay | Low | ⭐⭐ | - |

---

## Technical Implementation Details

### 1. Dense Embeddings Upgrade

```python
# Replace character n-grams with sentence-transformers
# model: sentence-transformers/all-MiniLM-L6-v2 (384-dim, fast)
# ONNX export for fast inference

class DenseEmbedder:
    def __init__(self, model_path: str):
        self.session = ort.InferenceSession(model_path)
        
    def embed(self, texts: List[str]) -> np.ndarray:
        # Batch inference
        pass
```

### 2. Hybrid Retrieval with RRF

```python
def hybrid_search(query, k=50, alpha=0.5):
    # BM25 candidates
    bm25_results = fts5_search(query, k=100)
    
    # Dense vector search
    query_vec = embed(query)
    dense_results = faiss_search(query_vec, k=100)
    
    # Reciprocal Rank Fusion
    scores = {}
    for rank, doc in enumerate(bm25_results):
        scores[doc.id] = scores.get(doc.id, 0) + alpha / (rank + 1)
    for rank, doc in enumerate(dense_results):
        scores[doc.id] = scores.get(doc.id, 0) + (1-alpha) / (rank + 1)
    
    return sorted(scores.items(), key=lambda x: -x[1])[:50]
```

### 3. Cross-Encoder Reranker

```python
def rerank(query, candidates, top_k=10):
    pairs = [(query, doc.title + " " + doc.body[:500]) for doc in candidates]
    scores = cross_encoder.predict(pairs)
    return sorted(zip(candidates, scores), key=lambda x: -x[1])[:top_k]
```

### 4. MMR Diversity

```python
def mmr_select(candidates, query_vec, lambda_mult=0.7, k=10):
    selected = []
    remaining = candidates.copy()
    
    while len(selected) < k and remaining:
        best = max(remaining, key=lambda doc: 
            lambda_mult * doc.score - (1-lambda_mult) * max(
                cosine_sim(doc.vec, s.vec) for s in selected) if selected else 0
        )
        selected.append(best)
        remaining.remove(best)
    return selected
```

---

## Migration Strategy

1. **Backward Compatibility**: Keep FTS5 as primary, add dense as optional enhancement
2. **Configurable Weights**: `hybrid_alpha=0.5` configurable
3. **Progressive Enhancement**: Works without dense vectors (falls back to BM25)
4. **Migration Script**: Re-embed all notes on upgrade

---

## Dependencies to Add

```
sentence-transformers>=2.2.0
onnxruntime>=1.15.0
faiss-cpu>=1.7.4  # or faiss-gpu
# OR: sqlite-vec (pure SQLite extension)
```

---

## Next Steps

1. **Immediate**: Add sentence-transformers embedding (replace char n-grams)
2. **Week 1**: Implement hybrid retrieval with RRF
3. **Week 2**: Add cross-encoder reranker
3. **Week 3**: Add query expansion + spell correction
4. **Week 4**: Add MMR diversity + freshness decay

---

## Acceptance Criteria

- [ ] Search latency < 100ms for 10k notes
- [ ] NDCG@10 improves by >15% vs BM25-only
- [ ] Recall@10 improves by >20% for semantic queries
- [ ] Latency p99 < 200ms with reranking
- [ ] Backward compatible (no dense vectors = BM25 only)
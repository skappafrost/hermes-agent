"""
Dense Semantic Embeddings Module for Obsidian Vault.

Provides dense semantic embeddings using sentence-transformers with ONNX Runtime
for fast inference. Includes FAISS vector index for efficient ANN search.
"""

from __future__ import annotations

import json
import logging
import math
import os
import pickle
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

import numpy as np

logger = logging.getLogger(__name__)

# Optional dependencies - lazy loaded
try:
    import faiss
    FAISS_AVAILABLE = True
except ImportError:
    FAISS_AVAILABLE = False
    logger.warning("FAISS not available, falling back to brute-force search")

try:
    from sentence_transformers import SentenceTransformer
    SENTENCE_TRANSFORMERS_AVAILABLE = True
except ImportError:
    SENTENCE_TRANSFORMERS_AVAILABLE = False
    logger.warning("sentence-transformers not available")

try:
    import torch
    TORCH_AVAILABLE = True
except ImportError:
    TORCH_AVAILABLE = False
    logger.warning("torch not available")

try:
    import onnxruntime as ort
    ORT_AVAILABLE = True
except ImportError:
    ORT_AVAILABLE = False
    logger.warning("ONNX Runtime not available")

try:
    import sqlite_vec
    SQLITE_VEC_AVAILABLE = True
except ImportError:
    SQLITE_VEC_AVAILABLE = False
    logger.warning("sqlite-vec not available")

# For dense embeddings availability check
DENSE_EMBEDDINGS_AVAILABLE = SENTENCE_TRANSFORMERS_AVAILABLE and TORCH_AVAILABLE


def _emb_present(emb) -> bool:
    """Safely check an embedding is non-empty (works for list or numpy array;
    numpy truthiness on a multi-element array is ambiguous and raises)."""
    if emb is None:
        return False
    if hasattr(emb, "size"):
        return emb.size > 0
    return len(emb) > 0


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_CROSS_ENCODER_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"
DEFAULT_EMBEDDING_DIM = 384
DEFAULT_BATCH_SIZE = 32
DEFAULT_MAX_SEQ_LENGTH = 512

# Phase 3 Configuration
DEFAULT_SYNONYM_EXPANSION = True
DEFAULT_FUZZY_MATCHING = True
DEFAULT_MMR_DIVERSITY = True
DEFAULT_FRESHNESS_DECAY = True
DEFAULT_FRESHNESS_HALF_LIFE_DAYS = 30.0
DEFAULT_MMR_LAMBDA = 0.7


# ---------------------------------------------------------------------------
# Embedding Model Interface
# ---------------------------------------------------------------------------

class EmbeddingModel:
    """Abstract base class for embedding models."""
    
    def __init__(self, dim: int):
        self.dim = dim
    
    def encode(self, texts: Union[str, List[str]], batch_size: int = 32, 
               show_progress_bar: bool = False, convert_to_numpy: bool = True) -> np.ndarray:
        raise NotImplementedError
    
    def encode_single(self, text: str) -> np.ndarray:
        return self.encode([text])[0]


class SentenceTransformerEmbedder(EmbeddingModel):
    """Wrapper for sentence-transformers models."""
    
    def __init__(self, model_name: str = DEFAULT_MODEL_NAME, 
                 device: Optional[str] = None,
                 dim: int = DEFAULT_EMBEDDING_DIM,
                 max_seq_length: int = DEFAULT_MAX_SEQ_LENGTH):
        if not SENTENCE_TRANSFORMERS_AVAILABLE:
            raise RuntimeError("sentence-transformers not installed")
        
        super().__init__(DEFAULT_EMBEDDING_DIM)
        self.model_name = model_name
        self.dim = dim
        self.device = device or ("cuda" if TORCH_AVAILABLE and torch.cuda.is_available() else "cpu")
        
        logger.info(f"Loading sentence-transformers model: {model_name} on {self.device}")
        self.model = SentenceTransformer(model_name, device=self.device)
        self.model.max_seq_length = max_seq_length
        
        # Verify dimensions
        test_emb = self.model.encode(["test"])
        actual_dim = len(test_emb[0])
        if actual_dim != dim:
            logger.warning(f"Model dimension ({actual_dim}) differs from expected ({dim})")
            self.dim = actual_dim
    
    def encode(self, texts: Union[str, List[str]], batch_size: int = 32,
               show_progress_bar: bool = False, convert_to_numpy: bool = True) -> np.ndarray:
        if isinstance(texts, str):
            texts = [texts]
        
        embeddings = self.model.encode(
            texts,
            batch_size=batch_size,
            show_progress_bar=show_progress_bar,
            convert_to_numpy=convert_to_numpy,
            normalize_embeddings=True  # L2 normalize for cosine similarity
        )
        return embeddings


class ONNXEmbedder(EmbeddingModel):
    """ONNX Runtime optimized embedder for faster inference."""
    
    def __init__(self, model_path: str, dim: int = DEFAULT_EMBEDDING_DIM):
        if not ORT_AVAILABLE:
            raise RuntimeError("ONNX Runtime not installed")
        
        super().__init__(dim)
        self.session = ort.InferenceSession(model_path, providers=['CPUExecutionProvider'])
        
        # Get input/output names
        self.input_names = [inp.name for inp in self.session.get_inputs()]
        self.output_names = [out.name for out in self.session.get_outputs()]
        
        # Get tokenizer (need to load separately or use simple tokenization)
        # For simplicity, we'll use sentence-transformers for tokenization
        from sentence_transformers import SentenceTransformer
        self.tokenizer = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2").tokenizer
    
    def encode(self, texts: Union[str, List[str]], batch_size: int = 32,
               show_progress_bar: bool = False, convert_to_numpy: bool = True) -> np.ndarray:
        if isinstance(texts, str):
            texts = [texts]
        
        all_embeddings = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i+batch_size]
            inputs = self.tokenizer(batch, padding=True, truncation=True, 
                                   max_length=512, return_tensors="np")
            
            # ONNX inference
            ort_inputs = {self.input_names[0]: inputs["input_ids"],
                         self.input_names[1]: inputs["attention_mask"]}
            outputs = self.session.run(self.output_names, ort_inputs)
            
            # Mean pooling
            embeddings = self._mean_pooling(outputs[0], inputs["attention_mask"])
            embeddings = self._normalize(embeddings)
            all_embeddings.append(embeddings)
        
        return np.vstack(all_embeddings) if all_embeddings else np.array([])
    
    def _mean_pooling(self, token_embeddings: np.ndarray, attention_mask: np.ndarray) -> np.ndarray:
        """Mean pooling with attention mask."""
        input_mask_expanded = np.expand_dims(attention_mask, -1).repeat(
            token_embeddings.shape[-1], axis=-1).astype(float)
        sum_embeddings = np.sum(token_embeddings * input_mask_expanded, axis=1)
        sum_mask = np.clip(np.sum(input_mask_expanded, axis=1), a_min=1e-9, a_max=None)
        return sum_embeddings / sum_mask


# ---------------------------------------------------------------------------
# FAISS Vector Index
# ---------------------------------------------------------------------------

class FAISSIndex:
    """FAISS-based vector index for fast ANN search.
    
    Supports tombstone-based deletion with search-time filtering and periodic rebuild.
    """
    
    def __init__(self, dim: int, index_type: str = "HNSW", 
                 metric: str = "cosine", m: int = 16, ef_construction: int = 200,
                 index_path: Optional[str] = None):
        if not FAISS_AVAILABLE:
            raise RuntimeError("FAISS not available")
        
        self.dim = dim
        self.index_type = index_type
        self.metric = metric
        
        if metric == "cosine":
            # For cosine similarity, normalize vectors and use inner product
            if index_type == "HNSW":
                inner = faiss.IndexHNSWFlat(dim, m)
                inner.hnsw.efConstruction = ef_construction
                inner.hnsw.efSearch = 64
                self.index = faiss.IndexIDMap2(inner)
            else:
                base_index = faiss.IndexFlatIP(dim)
                self.index = faiss.IndexIDMap2(base_index)
        else:
            # L2 distance
            base_index = faiss.IndexHNSWFlat(dim, m) if index_type == "HNSW" else faiss.IndexFlatL2(dim)
            if index_type == "HNSW":
                base_index.hnsw.efConstruction = ef_construction
                base_index.hnsw.efSearch = 64
            self.index = faiss.IndexIDMap2(base_index)
        
        # Tombstone mechanism for deleted/updated vectors
        self._tombstones: Set[int] = set()
        self._id_to_slug: Dict[int, str] = {}  # FAISS internal ID -> slug
        self._slug_to_id: Dict[str, int] = {}  # slug -> FAISS internal ID
        self._rebuild_threshold = 0.25  # Rebuild when 25% of vectors are tombstoned
        self._next_id = 0
        
        # Load existing index if path provided
        if index_path and os.path.exists(index_path):
            self.load(index_path)
    
    def _allocate_id(self, slug: str) -> int:
        """Allocate a new FAISS internal ID for a slug."""
        if slug in self._slug_to_id:
            return self._slug_to_id[slug]
        fid = self._next_id
        self._next_id += 1
        self._slug_to_id[slug] = fid
        self._id_to_slug[fid] = slug
        return fid
    
    def add(self, vectors: np.ndarray, ids: Optional[np.ndarray] = None, slugs: Optional[List[str]] = None):
        """Add vectors to index.
        
        Args:
            vectors: Embedding vectors to add
            ids: Optional explicit FAISS IDs (deprecated, use slugs instead)
            slugs: List of slugs corresponding to vectors (preferred)
        """
        if vectors.dtype != np.float32:
            vectors = vectors.astype(np.float32)

        # faiss expects a 2-D (n, dim) array; reshape a 1-D (dim,) input.
        if vectors.ndim == 1:
            vectors = vectors.reshape(1, -1)

        # Normalize for cosine similarity
        faiss.normalize_L2(vectors)
        
        if slugs is not None:
            # Preferred: use slugs for ID allocation
            faiss_ids = np.array([self._allocate_id(slug) for slug in slugs], dtype=np.int64)
            self.index.add_with_ids(vectors, faiss_ids)
        elif ids is not None:
            # Legacy: use explicit IDs
            self.index.add_with_ids(vectors, ids.astype(np.int64))
            # Update internal mappings if IDs are new
            for fid in ids:
                fid_int = int(fid)
                if fid_int >= self._next_id:
                    self._next_id = fid_int + 1
        else:
            # Auto-assign sequential IDs
            faiss_ids = np.arange(self._next_id, self._next_id + len(vectors), dtype=np.int64)
            self.index.add_with_ids(vectors, faiss_ids)
            self._next_id += len(vectors)
    
    def search(self, query_vectors: np.ndarray, k: int = 10, filter_tombstones: bool = True) -> Tuple[np.ndarray, np.ndarray]:
        """Search for k nearest neighbors.
        
        Args:
            query_vectors: Query embedding vectors
            k: Number of results to return
            filter_tombstones: If True, filter out tombstoned vectors from results
            
        Returns:
            distances: shape (n_queries, k)
            indices: shape (n_queries, k)
        """
        if query_vectors.dtype != np.float32:
            query_vectors = query_vectors.astype(np.float32)

        # faiss.normalize_L2 requires a 2-D (n, dim) array. encode_single() and
        # similar callers may hand over a 1-D (dim,) vector, which crashes the
        # C wrapper with "tuple index out of range". Reshape defensively.
        if query_vectors.ndim == 1:
            query_vectors = query_vectors.reshape(1, -1)

        faiss.normalize_L2(query_vectors)
        
        # Search with extra candidates to account for tombstones
        search_k = k * 3 if filter_tombstones and self._tombstones else k
        search_k = min(search_k, self.index.ntotal)
        
        if search_k <= 0:
            return np.array([]), np.array([])
        
        distances, indices = self.index.search(query_vectors, search_k)
        
        if filter_tombstones and self._tombstones:
            # Filter out tombstoned results
            filtered_distances = []
            filtered_indices = []
            for i in range(distances.shape[0]):
                row_dists = []
                row_indices = []
                for dist, idx in zip(distances[i], indices[i]):
                    if idx >= 0 and idx not in self._tombstones:
                        row_dists.append(dist)
                        row_indices.append(idx)
                        if len(row_indices) >= k:
                            break
                # Pad if needed
                while len(row_indices) < k:
                    row_dists.append(float('inf'))
                    row_indices.append(-1)
                filtered_distances.append(row_dists[:k])
                filtered_indices.append(row_indices[:k])
            distances = np.array(filtered_distances, dtype=np.float32)
            indices = np.array(filtered_indices, dtype=np.int64)
        
        return distances[:, :k], indices[:, :k]
    
    def remove(self, ids: np.ndarray):
        """Mark vectors as deleted (tombstone) instead of physical removal."""
        for fid in ids:
            fid_int = int(fid)
            self._tombstones.add(fid_int)
            # Clean up mappings
            if fid_int in self._id_to_slug:
                slug = self._id_to_slug.pop(fid_int)
                self._slug_to_id.pop(slug, None)
        
        # Check if rebuild is needed
        if self.index.ntotal > 0 and len(self._tombstones) / self.index.ntotal > self._rebuild_threshold:
            logger.info(f"FAISS tombstone ratio exceeded threshold, triggering rebuild ({len(self._tombstones)}/{self.index.ntotal})")
            self._rebuild()
    
    def remove_by_slug(self, slugs: List[str]):
        """Remove vectors by slug (convenience method)."""
        fids = [self._slug_to_id[slug] for slug in slugs if slug in self._slug_to_id]
        if fids:
            self.remove(np.array(fids, dtype=np.int64))
    
    def _rebuild(self):
        """Rebuild index excluding tombstoned vectors."""
        if not self._tombstones:
            return
        
        logger.info(f"Rebuilding FAISS index, removing {len(self._tombstones)} tombstoned vectors")
        
        # Get all valid vectors
        valid_ids = []
        valid_vectors = []
        for fid in range(self._next_id):
            if fid not in self._tombstones:
                # Reconstruct vector from index (this is expensive but necessary)
                try:
                    vec = self.index.reconstruct(fid)
                    valid_ids.append(fid)
                    valid_vectors.append(vec)
                except Exception:
                    pass
        
        # Create new index (always wrapped in IndexIDMap2 so add_with_ids works
        # for both HNSW and Flat backends — see __init__). Set HNSW tuning params
        # on the inner index BEFORE wrapping (IndexIDMap2 hides .hnsw).
        if self.metric == "cosine":
            base_index = faiss.IndexHNSWFlat(self.dim, 16) if self.index_type == "HNSW" else faiss.IndexFlatIP(self.dim)
        else:
            base_index = faiss.IndexHNSWFlat(self.dim, 16) if self.index_type == "HNSW" else faiss.IndexFlatL2(self.dim)
        if self.index_type == "HNSW":
            base_index.hnsw.efConstruction = 200
            base_index.hnsw.efSearch = 64
        new_index = faiss.IndexIDMap2(base_index)

        if valid_vectors:
            vectors = np.array(valid_vectors, dtype=np.float32)
            faiss.normalize_L2(vectors)
            new_index.add_with_ids(vectors, np.array(valid_ids, dtype=np.int64))
        
        # Replace index
        self.index = new_index
        self._tombstones.clear()
        self._next_id = max(valid_ids) + 1 if valid_ids else 0
        # Rebuild mappings
        self._id_to_slug = {fid: self._id_to_slug[fid] for fid in valid_ids if fid in self._id_to_slug}
        self._slug_to_id = {slug: fid for fid, slug in self._id_to_slug.items()}
    
    def save(self, path: str):
        """Save index and tombstone metadata to disk."""
        faiss.write_index(self.index, path)
        # Save tombstone metadata separately
        meta_path = path + ".meta"
        meta = {
            "tombstones": list(self._tombstones),
            "id_to_slug": {str(k): v for k, v in self._id_to_slug.items()},
            "slug_to_id": self._slug_to_id,
            "next_id": self._next_id
        }
        with open(meta_path, 'w') as f:
            json.dump(meta, f)
    
    def load(self, path: str):
        """Load index and tombstone metadata from disk.
        
        Also validates index against authoritative SQLite state if available.
        """
        self.index = faiss.read_index(path)
        # Load tombstone metadata
        meta_path = path + ".meta"
        if os.path.exists(meta_path):
            with open(meta_path, 'r') as f:
                meta = json.load(f)
            self._tombstones = set(meta.get("tombstones", []))
            self._id_to_slug = {int(k): v for k, v in meta.get("id_to_slug", {}).items()}
            self._slug_to_id = meta.get("slug_to_id", {})
            self._next_id = meta.get("next_id", 0)
        else:
            # Legacy index without metadata - assume clean
            self._tombstones.clear()
            self._id_to_slug.clear()
            self._slug_to_id.clear()
            self._next_id = self.index.ntotal
        
        # Cold-start validation: check if FAISS index is consistent
        # This is a basic check - more thorough validation happens in EmbeddingPipeline
        if self.needs_rebuild():
            logger.warning("FAISS index needs rebuild due to high tombstone ratio")
    
    @property
    def ntotal(self) -> int:
        return self.index.ntotal - len(self._tombstones)
    
    def get_active_count(self) -> int:
        """Get count of non-tombstoned vectors."""
        return self.index.ntotal - len(self._tombstones)
    
    def get_tombstone_count(self) -> int:
        return len(self._tombstones)
    
    def needs_rebuild(self) -> bool:
        """Check if rebuild is needed based on tombstone ratio."""
        if self.index.ntotal == 0:
            return False
        return len(self._tombstones) / self.index.ntotal > self._rebuild_threshold


# ---------------------------------------------------------------------------
# SQLite-vec Index (Alternative to FAISS)
# ---------------------------------------------------------------------------

class SQLiteVecIndex:
    """SQLite-vec based vector index using sqlite-vec extension."""
    
    def __init__(self, db_path: str, dim: int, table_name: str = "vec_index"):
        if not SQLITE_VEC_AVAILABLE:
            raise RuntimeError("sqlite-vec not available")
        
        self.dim = dim
        self.table_name = table_name
        self.conn = sqlite3.connect(db_path)
        self.conn.enable_load_extension(True)
        import sqlite_vec
        sqlite_vec.load(self.conn)
        
        # Create virtual table
        self.conn.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS {table_name} 
            USING vec0(embedding float[{dim}])
        """)
        self.conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {table_name}_meta (
                rowid INTEGER PRIMARY KEY,
                slug TEXT UNIQUE,
                metadata TEXT
            )
        """)
        self.conn.commit()
    
    def add(self, vectors: np.ndarray, slugs: List[str]):
        """Add vectors with associated slugs."""
        if vectors.dtype != np.float32:
            vectors = vectors.astype(np.float32)
        
        # Normalize for cosine similarity
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        vectors = vectors / np.maximum(norms, 1e-9)
        
        # Insert into vec table (auto-assigns rowids)
        for i, (vec, slug) in enumerate(zip(vectors, slugs)):
            # Insert into vec table
            self.conn.execute(f"INSERT INTO {self.table_name} (embedding) VALUES (?)", 
                            [vec.tobytes()])
            rowid = self.conn.lastrowid
            # Store metadata
            self.conn.execute(
                f"INSERT INTO {self.table_name}_meta (rowid, slug, metadata) VALUES (?, ?, ?)",
                (rowid, slugs[i], json.dumps({}))
            )
        self.conn.commit()
    
    def search(self, query_vector: np.ndarray, k: int = 10) -> List[Tuple[str, float]]:
        """Search for k nearest neighbors."""
        if query_vector.dtype != np.float32:
            query_vector = query_vector.astype(np.float32)
        
        # Normalize
        query_vector = query_vector / (np.linalg.norm(query_vector) + 1e-9)
        
        # Search using sqlite-vec
        query_bytes = query_vector.astype(np.float32).tobytes()
        rows = self.conn.execute(
            f"""
            SELECT m.slug, 1.0 - distance 
            FROM {self.table_name} v 
            JOIN {self.table_name}_meta m ON v.rowid = m.rowid
            WHERE v.embedding MATCH ? 
            AND k = ?
            ORDER BY distance
            LIMIT ?
        """, [query_vector.tobytes(), k]).fetchall()
        
        return [(slug, 1.0 - dist) for slug, dist in rows]  # Convert distance to similarity
    
    def remove(self, slugs: List[str]):
        """Remove vectors by slug."""
        placeholders = ",".join(["?" for _ in slugs])
        self.conn.execute(
            f"DELETE FROM {self.table_name}_meta WHERE slug IN ({','.join(['?']*len(slugs))})",
            slugs
        )
        # Note: sqlite-vec doesn't easily support deletion from vec table
        # Would need to rebuild or mark as deleted
        self.conn.commit()
    
    def close(self):
        self.conn.close()


# ---------------------------------------------------------------------------
# Hybrid Vector Store (Abstraction Layer)
# ---------------------------------------------------------------------------

class VectorStore:
    """Unified interface for vector storage and search."""
    
    def __init__(self, dim: int, backend: str = "faiss", 
                 db_path: Optional[str] = None, index_path: Optional[str] = None):
        self.dim = dim
        self.backend = backend
        
        if backend == "faiss":
            if not FAISS_AVAILABLE:
                raise RuntimeError("FAISS not available")
            self.store = FAISSIndex(dim, index_path=index_path)
        elif backend == "sqlite-vec":
            if not SQLITE_VEC_AVAILABLE:
                raise RuntimeError("sqlite-vec not available")
            if not db_path:
                raise ValueError("db_path required for sqlite-vec backend")
            self.store = SQLiteVecIndex(db_path, dim)
        else:
            raise ValueError(f"Unknown backend: {backend}")
    
    def add(self, vectors: np.ndarray, slugs: List[str]):
        """Add vectors with associated slugs."""
        if vectors.dtype != np.float32:
            vectors = vectors.astype(np.float32)
        
        # Normalize for cosine similarity
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        vectors = vectors / np.maximum(norms, 1e-9)
        
        if hasattr(self.store, 'add'):
            self.store.add(vectors, slugs=slugs)
        else:
            # Fallback for FAISS (no slug support directly)
            pass
    
    def search(self, query_vector: np.ndarray, k: int = 10) -> List[Tuple[str, float]]:
        """Search for k nearest neighbors.

        Normalizes the backend return shape into a list of (slug, similarity)
        tuples. The FAISS backend returns (distances, indices) numpy arrays;
        the sqlite-vec backend returns (slug, 1.0 - distance) rows. Both are
        unified here so callers (e.g. HybridSearcher._rrf_fusion) always get
        (slug, score) tuples.
        """
        if query_vector.dtype != np.float32:
            query_vector = query_vector.astype(np.float32)

        if hasattr(self.store, 'search'):
            result = self.store.search(query_vector, k)

            # FAISS backend returns (distances, indices) numpy arrays.
            if isinstance(result, tuple) and len(result) == 2 and all(
                isinstance(r, np.ndarray) for r in result
            ):
                distances, indices = result
                if distances.size == 0:
                    return []
                if distances.ndim == 1:
                    distances = distances.reshape(1, -1)
                    indices = indices.reshape(1, -1)
                out = []
                id_to_slug = getattr(self.store, "_id_to_slug", {})
                for row_d, row_i in zip(distances, indices):
                    for d, idx in zip(row_d, row_i):
                        if int(idx) < 0:
                            continue
                        slug = id_to_slug.get(int(idx))
                        if slug is None:
                            continue
                        # FAISS cosine index stores inner-product scores, which
                        # equal cosine similarity for L2-normalized vectors.
                        out.append((slug, float(d)))
                return out

            # sqlite-vec backend already returns (slug, score)-like rows.
            return result

        return []
    
    def remove(self, slugs: List[str]):
        """Remove vectors by slugs."""
        if hasattr(self.store, 'remove_by_slug'):
            self.store.remove_by_slug(slugs)
        elif hasattr(self.store, 'remove'):
            self.store.remove(slugs)
    
    def save(self, path: str):
        if hasattr(self.store, 'save'):
            self.store.save(path)
    
    def load(self, path: str):
        if hasattr(self.store, 'load'):
            self.store.load(path)
    
    @property
    def ntotal(self) -> int:
        if hasattr(self.store, 'ntotal'):
            return self.store.ntotal
        return 0


# ---------------------------------------------------------------------------
# Embedding Pipeline
# ---------------------------------------------------------------------------

class EmbeddingPipeline:
    """Complete embedding pipeline with model, vector index, and persistence."""
    
    def __init__(self, 
                 model_name: str = DEFAULT_MODEL_NAME,
                 dim: int = DEFAULT_EMBEDDING_DIM,
                 index_backend: str = "faiss",
                 index_path: Optional[str] = None,
                 cache_dir: Optional[str] = None):
        self.dim = dim
        self.cache_dir = Path(cache_dir) if cache_dir else None
        
        # Initialize embedding model
        try:
            self.model = SentenceTransformerEmbedder(model_name)
            actual_dim = self.model.dim
            if actual_dim != dim:
                logger.warning(f"Model dim ({actual_dim}) != expected ({dim}), using {actual_dim}")
                self.dim = actual_dim
            else:
                self.dim = dim
        except Exception as e:
            logger.warning(f"Failed to load sentence-transformers: {e}, using fallback")
            self.model = None
            self.dim = dim
        
        # Initialize vector store
        self.vector_store = VectorStore(self.dim, backend="faiss", index_path=index_path)
        
        # Cache for embeddings
        self._embedding_cache: Dict[str, np.ndarray] = {}
        self._cache_lock = threading.Lock()
        
        # Load existing index if available
        if index_path and Path(index_path).exists():
            try:
                self.vector_store.load(index_path)
                logger.info(f"Loaded vector index from {index_path} ({self.vector_store.ntotal} vectors)")
                
                # Cold-start validation: check if FAISS index needs rebuild
                if hasattr(self.vector_store.store, 'needs_rebuild') and self.vector_store.store.needs_rebuild():
                    logger.warning("FAISS index has high tombstone ratio, rebuilding...")
                    self.rebuild_index({})  # Will be populated from vault later
            except Exception as e:
                logger.warning(f"Failed to load index: {e}")
    
    def encode(self, texts: Union[str, List[str]]) -> np.ndarray:
        """Encode text(s) to embeddings."""
        if self.model is None:
            raise RuntimeError("No embedding model available")
        return self.model.encode(texts)
    
    def encode_single(self, text: str) -> np.ndarray:
        return self.encode([text])[0]
    
    def add_note(self, slug: str, text: str, metadata: Optional[Dict] = None):
        """Add or update a note's embedding."""
        embedding = self.encode_single(text)
        
        with self._cache_lock:
            self._embedding_cache[slug] = embedding
        
        self.vector_store.add(embedding.reshape(1, -1), [slug])
    
    def remove_note(self, slug: str):
        """Remove a note's embedding."""
        with self._cache_lock:
            self._embedding_cache.pop(slug, None)
        self.vector_store.remove([slug])
    
    def search(self, query: str, k: int = 10) -> List[Tuple[str, float]]:
        """Search for similar notes."""
        query_embedding = self.encode_single(query)
        return self.vector_store.search(query_vector=query_embedding, k=k)
    
    def rebuild_index(self, notes: Dict[str, Dict[str, Any]]):
        """Rebuild entire index from notes dictionary."""
        # Clear existing
        self.vector_store = VectorStore(self.dim, backend="faiss")
        
        # Batch encode all notes
        texts = []
        slugs = []
        for slug, note_data in notes.items():
            text = f"{note_data.get('title', '')} {note_data.get('body', '')}"
            texts.append(text)
            slugs.append(slug)
        
        if texts:
            embeddings = self.encode(texts)
            self.vector_store.add(embeddings, slugs=slugs)
    
    def save(self, path: str):
        """Save index to disk."""
        self.vector_store.save(path)
    
    def get_stats(self) -> Dict[str, Any]:
        return {
            "dim": self.dim,
            "total_vectors": self.vector_store.ntotal,
            "cache_size": len(self._embedding_cache),
            "model_loaded": self.model is not None
        }


# ---------------------------------------------------------------------------
# Cross-Encoder Reranker (Phase 2)
# ---------------------------------------------------------------------------

class CrossEncoderReranker:
    """Cross-encoder reranker for improving search precision."""
    
    def __init__(self, 
                 model_name: str = DEFAULT_CROSS_ENCODER_MODEL,
                 device: Optional[str] = None,
                 max_length: int = 512):
        if not SENTENCE_TRANSFORMERS_AVAILABLE:
            raise RuntimeError("sentence-transformers not installed")
        
        from sentence_transformers import CrossEncoder
        
        self.model_name = model_name
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        
        logger.info(f"Loading cross-encoder model: {model_name} on {self.device}")
        self.model = CrossEncoder(model_name, device=self.device, max_length=512)
        
        logger.info(f"Cross-encoder reranker loaded: {model_name}")
    
    def rerank(self, query: str, candidates: List[Tuple[str, float]], 
               top_k: int = 10, doc_getter: Optional[callable] = None) -> List[Tuple[str, float]]:
        """
        Rerank candidates using cross-encoder.
        
        Args:
            query: Search query
            candidates: List of (slug, score) tuples from hybrid search
            top_k: Number of results to return after reranking
            doc_getter: Function to get document text from slug
            
        Returns:
            List of (slug, score) tuples reranked by cross-encoder
        """
        if not candidates:
            return []
        
        # Limit candidates for reranking (cross-encoder is slower)
        candidates = candidates[:50]
        
        # Prepare pairs for cross-encoder
        pairs = []
        slugs = []
        
        for slug, score in candidates:
            slugs.append(slug)
            # Get document text for cross-encoder
            doc_text = self._get_doc_text(slug)
            pairs.append([query, doc_text[:512]])  # Truncate to 512 tokens
        
        if not pairs:
            return []
        
        try:
            # Cross-encoder prediction
            scores = self.model.predict(pairs, batch_size=32, show_progress_bar=False)
            
            # Combine with original scores (weighted combination)
            reranked = []
            for slug, orig_score, cross_score in zip(slugs, [c[1] for c in candidates], scores):
                # Combine scores: 70% cross-encoder, 30% original hybrid score
                combined_score = 0.7 * cross_score + 0.3 * orig_score
                reranked.append((slug, combined_score))
            
            # Sort by combined score
            reranked.sort(key=lambda x: -x[1])
            
            return reranked[:top_k]
            
        except Exception as e:
            logger.warning(f"Cross-encoder reranking failed: {e}")
            # Fallback to original scores
            return candidates[:top_k]
    
    def _get_doc_text(self, slug: str) -> str:
        """Get document text for cross-encoder. Override or extend as needed."""
        # This should be overridden or extended with actual document retrieval
        # For now, return slug as placeholder
        return slug.replace("-", " ").replace("_", " ")


# ---------------------------------------------------------------------------
# Phase 3: Query Expansion, Fuzzy Matching, MMR Diversity, Freshness Decay
# ---------------------------------------------------------------------------

class QueryExpander:
    """Query expansion with synonyms and entity extraction."""
    
    def __init__(self, model_name: str = DEFAULT_MODEL_NAME, 
                 use_synonyms: bool = True):
        self.use_synonyms = use_synonyms
        # Simple synonym dictionary (can be extended with WordNet or word embeddings)
        self.synonyms = {
            "note": ["memo", "jot", "record", "entry"],
            "create": ["make", "add", "new", "generate"],
            "search": ["find", "lookup", "query", "locate"],
            "delete": ["remove", "delete", "erase", "drop"],
            "update": ["modify", "edit", "change", "alter"],
            "project": ["task", "initiative", "work", "venture"],
            "meeting": ["call", "session", "sync", "standup"],
            "document": ["doc", "file", "note", "record"],
            "team": ["group", "crew", "squad", "unit"],
        }
        
        # Entity patterns (regex for common entities)
        import re
        self.entity_patterns = {
            "date": re.compile(r'\b\d{4}-\d{2}-\d{2}\b'),
            "email": re.compile(r'\b[\w.+-]+@[\w.-]+\.\w+\b'),
            "url": re.compile(r'https?://\S+'),
            "tag": re.compile(r'#[a-zA-Z0-9_-]+'),
            "mention": re.compile(r'@[a-zA-Z0-9_-]+'),
        }
    
    def expand(self, query: str) -> List[str]:
        """Expand query with synonyms and related terms."""
        tokens = query.lower().split()
        expanded = set([query])
        
        if self.use_synonyms:
            for token in tokens:
                for syn in self.synonyms.get(token, []):
                    # Replace token with synonym
                    expanded.add(query.replace(token, syn))
                    expanded.add(query.replace(token, syn.capitalize()))
        
        # Add entity-aware expansions
        for entity_type, pattern in self.entity_patterns.items():
            matches = pattern.findall(query)
            for match in matches:
                # Add entity-aware variations
                expanded.add(query.replace(match, f"[{entity_type}:{match}]"))
        
        return list(expanded)


class SpellCorrector:
    """Spell correction with Levenshtein distance for fuzzy matching."""
    
    def __init__(self, dictionary: Optional[List[str]] = None, 
                 max_distance: int = 2):
        self.max_distance = max_distance
        self.dictionary = set(dictionary) if dictionary else set()
        
        # Build vocabulary from common words if no dictionary provided
        if not self.dictionary:
            self._build_default_dictionary()
    
    def _build_default_dictionary(self):
        """Build a basic English dictionary."""
        common_words = [
            "the", "be", "to", "of", "and", "a", "in", "that", "have", "i",
            "it", "for", "not", "on", "with", "he", "as", "you", "do", "at",
            "this", "but", "his", "by", "from", "they", "we", "say", "her", "she",
            "or", "an", "will", "my", "one", "all", "would", "there", "their",
            "what", "so", "up", "out", "if", "about", "who", "get", "which", "go",
            "me", "when", "make", "can", "like", "time", "no", "just", "him", "know",
            "take", "person", "into", "year", "your", "good", "some", "could", "them",
            "see", "other", "than", "then", "now", "look", "only", "come", "its",
            "over", "think", "also", "back", "after", "use", "two", "how", "our",
            "work", "first", "well", "way", "even", "new", "want", "because", "any",
            "these", "give", "day", "most", "us", "is", "name", "very", "through",
            "just", "form", "much", "great", "think", "say", "help", "low", "line",
            "differ", "turn", "cause", "much", "mean", "before", "move", "right",
            "boy", "old", "too", "same", "tell", "does", "set", "three", "want",
            "air", "well", "also", "play", "small", "end", "put", "home", "read",
            "hand", "port", "large", "spell", "add", "even", "land", "here", "must",
            "big", "high", "such", "follow", "act", "why", "ask", "men", "change",
            "went", "light", "kind", "off", "need", "house", "picture", "try", "us",
            "again", "animal", "point", "mother", "world", "near", "build", "self",
            "earth", "father", "head", "stand", "own", "page", "should", "country",
            "found", "answer", "school", "grow", "study", "still", "learn", "plant",
            "cover", "food", "sun", "four", "between", "state", "keep", "eye", "never",
            "last", "let", "thought", "city", "tree", "cross", "farm", "hard", "start",
            "might", "story", "saw", "far", "sea", "draw", "left", "late", "run",
            "don't", "while", "press", "close", "night", "real", "life", "few", "north",
            "note", "create", "search", "find", "delete", "update", "project", "meeting",
            "document", "team", "task", "project", "file", "folder", "tag", "category",
            "link", "search", "query", "filter", "sort", "filter", "group", "archive",
            "programming", "program", "programs", "programmer", "programmers",
        ]
        self.dictionary = set(common_words)
    
    @staticmethod
    def levenshtein_distance(s1: str, s2: str) -> int:
        """Calculate Levenshtein distance between two strings."""
        if len(s1) < len(s2):
            return SpellCorrector.levenshtein_distance(s2, s1)
        
        if len(s2) == 0:
            return len(s1)
        
        previous_row = list(range(len(s2) + 1))
        for i, c1 in enumerate(s1):
            current_row = [i + 1]
            for j, c2 in enumerate(s2):
                insertions = previous_row[j + 1] + 1
                deletions = current_row[j] + 1
                substitutions = previous_row[j] + (c1 != c2)
                current_row.append(min(insertions, deletions, substitutions))
            previous_row = current_row
        
        return previous_row[-1]
    
    def correct(self, word: str) -> str:
        """Find the closest word in dictionary."""
        if word in self.dictionary:
            return word
        
        candidates = []
        for dict_word in self.dictionary:
            dist = self.levenshtein_distance(word.lower(), dict_word.lower())
            if dist <= self.max_distance:
                candidates.append((dist, dict_word))
        
        if candidates:
            return min(candidates, key=lambda x: x[0])[1]
        return word
    
    def correct_query(self, query: str) -> str:
        """Correct spelling in entire query."""
        words = query.split()
        corrected = []
        corrections = []  # Track corrections made
        for word in words:
            corrected_word = self.correct(word)
            corrected.append(corrected_word)
            if corrected_word != word:
                corrections.append((word, corrected_word))
        return " ".join(corrected), corrections

    def get_corrections(self, query: str) -> List[Tuple[str, str]]:
        """Get list of corrections that would be made without applying them."""
        words = query.split()
        corrections = []
        for word in words:
            corrected = self.correct(word)
            if corrected != word:
                corrections.append((word, corrected))
        return corrections


class MMRDiversity:
    """Maximal Marginal Relevance for result diversification."""
    
    def __init__(self, lambda_mult: float = 0.7):
        self.lambda_mult = lambda_mult
    
    def select(self, candidates: List[Tuple[str, float, np.ndarray]], 
               k: int, query_vec: Optional[np.ndarray] = None) -> List[Tuple[str, float]]:
        """Select k diverse results using MMR.
        
        MMR = lambda * relevance - (1 - lambda) * max_similarity_to_selected
        
        Args:
            candidates: List of (slug, score, embedding) tuples
            k: Number of results to select
            query_vec: Query embedding for relevance scoring
            
        Returns:
            List of (slug, score) tuples selected by MMR
        """
        if not candidates:
            return []
        
        if len(candidates) <= k:
            return [(slug, score) for slug, score, _ in candidates[:k]]
        
        selected = []
        remaining = candidates[:]
        
        while len(selected) < k and remaining:
            best = None
            best_score = -float('inf')
            
            for i, (slug, score, vec) in enumerate(remaining):
                # Relevance score
                relevance = score
                
                # Diversity penalty
                diversity_penalty = 0
                if selected:
                    max_sim = max(
                        cosine_similarity(vec, s_vec) 
                        for _, _, s_vec in selected
                    )
                    diversity_penalty = (1 - self.lambda_mult) * max_sim
                else:
                    diversity_penalty = 0
                
                mmr_score = self.lambda_mult * relevance - diversity_penalty
                
                if mmr_score > best_score:
                    best_score = mmr_score
                    best = i
            
            if best is not None:
                selected.append(remaining.pop(best))
            else:
                break
        
        return [(slug, score) for slug, score, _ in selected]


class ExponentialFreshnessDecay:
    """Exponential freshness decay for recency ranking."""
    
    def __init__(self, half_life_days: float = 30.0):
        self.half_life_days = half_life_days
        self.lambda_decay = math.log(2) / half_life_days
    
    def decay_factor(self, age_days: float) -> float:
        """Calculate decay factor based on age in days."""
        return math.exp(-self.lambda_decay * age_days)
    
    def apply_decay(self, score: float, age_days: float) -> float:
        """Apply exponential decay to score based on age."""
        return score * self.decay_factor(age_days)
    
    def apply_to_results(self, results: List[Tuple[str, float, float]], 
                         now: float = None) -> List[Tuple[str, float]]:
        """Apply decay to results with (slug, score, timestamp) tuples."""
        if now is None:
            now = time.time()
        
        decayed = []
        for slug, score, timestamp in results:
            age_days = (now - timestamp) / 86400  # Convert seconds to days
            decayed_score = self.apply_decay(score, age_days)
            decayed.append((slug, decayed_score))
        
        # Re-sort by decayed score
        decayed.sort(key=lambda x: -x[1])
        return decayed


class ResultClustering:
    """Result clustering/grouping for topic-based organization."""
    
    def __init__(self, min_cluster_size: int = 2, similarity_threshold: float = 0.7):
        self.min_cluster_size = min_cluster_size
        self.similarity_threshold = similarity_threshold
    
    def cluster(self, results: List[Tuple[str, float, np.ndarray]]) -> List[List[Tuple[str, float, np.ndarray]]]:
        """Cluster results by semantic similarity.
        
        Args:
            results: List of (slug, score, embedding) tuples
            
        Returns:
            List of clusters, each containing similar results
        """
        if not results:
            return []
        
        if len(results) <= 1:
            return [results]
        
        # Simple hierarchical clustering
        clusters = []
        remaining = results[:]
        
        while remaining:
            # Start new cluster with first item
            seed = remaining.pop(0)
            cluster = [seed]
            
            # Find similar items
            i = 0
            while i < len(remaining):
                slug, score, vec = remaining[i]
                # Check similarity with cluster members
                max_sim = max(
                    cosine_similarity(vec, member[2]) 
                    for member in cluster
                )
                
                if max_sim >= self.similarity_threshold:
                    cluster.append(remaining.pop(i))
                else:
                    i += 1
            
            if len(cluster) >= self.min_cluster_size:
                clusters.append(cluster)
            else:
                # Too small, add back to remaining as individual items
                remaining.extend(cluster)
        
        # Add remaining as individual clusters
        for item in remaining:
            clusters.append([item])
        
        return clusters
    
    def get_cluster_representatives(self, clusters: List[List[Tuple[str, float, np.ndarray]]]) -> List[Tuple[str, float, np.ndarray]]:
        """Get representative (highest score) from each cluster."""
        representatives = []
        for cluster in clusters:
            if cluster:
                # Sort by score and take highest
                best = max(cluster, key=lambda x: x[1])
                representatives.append(best)
        return representatives


# ---------------------------------------------------------------------------
# Hybrid Search Integration (Updated for Phase 4)
# ---------------------------------------------------------------------------

class HybridSearcher:
    """Hybrid search combining FTS5 (BM25) and Dense Vector Search with RRF.
    
    Pipeline:
    1. Query expansion (synonyms, entity extraction)
    2. Spell correction (fuzzy matching)
    3. Get BM25 results from FTS5
    4. Get dense vector results from FAISS
    5. Fuse with Reciprocal Rank Fusion (RRF)
    5. Optional: Cross-encoder reranking for improved precision
    6. Apply MMR diversity filtering
    6. Apply freshness decay
    7. Apply result clustering (Phase 4)
    7. Apply filters (category, tags)
    7. Sort, paginate, return note objects
    
    RRF Formula: score = sum(weight_i / (rank_i + k))
    """
    
    def __init__(self, vault_index: Any, embedding_pipeline: EmbeddingPipeline,
                 bm25_weight: float = 0.5, dense_weight: float = 0.5,
                 rrf_k: int = 60,
                 use_reranker: bool = True,
                 reranker_model: str = DEFAULT_CROSS_ENCODER_MODEL,
                 rerank_top_k: int = 50,
                 rerank_top_k_final: int = 10,
                 # Phase 3 features
                 use_synonym_expansion: bool = True,
                 use_fuzzy_matching: bool = True,
                 use_mmr_diversity: bool = True,
                 use_freshness_decay: bool = True,
                 freshness_half_life_days: float = 30.0,
                 mmr_lambda: float = 0.7,
                 # Phase 4 features
                 use_result_clustering: bool = True,
                 cluster_min_size: int = 2,
                 cluster_similarity_threshold: float = 0.7):
        self.vault_index = vault_index
        self.embedding_pipeline = embedding_pipeline
        self.bm25_weight = bm25_weight
        self.dense_weight = dense_weight
        self.rrf_k = rrf_k
        self.use_reranker = use_reranker
        self.reranker_model = reranker_model
        self.rerank_top_k = rerank_top_k
        self.rerank_top_k_final = rerank_top_k_final
        
        # Phase 3 features
        self.use_synonym_expansion = use_synonym_expansion
        self.use_fuzzy_matching = use_fuzzy_matching
        self.use_mmr_diversity = use_mmr_diversity
        self.use_freshness_decay = use_freshness_decay
        self.freshness_half_life_days = freshness_half_life_days
        self.mmr_lambda = mmr_lambda
        
        # Phase 4 features
        self.use_result_clustering = use_result_clustering
        self.cluster_min_size = cluster_min_size
        self.cluster_similarity_threshold = cluster_similarity_threshold
        
        # Initialize Phase 3 components
        self.query_expander = QueryExpander(use_synonyms=use_synonym_expansion)
        self.spell_corrector = SpellCorrector()
        self.mmr_diversity = MMRDiversity(lambda_mult=mmr_lambda)
        self.freshness_decay = ExponentialFreshnessDecay(half_life_days=freshness_half_life_days)
        
        # Phase 4: Result clustering
        self.use_result_clustering = use_result_clustering
        self.result_clustering = ResultClustering(
            min_cluster_size=cluster_min_size,
            similarity_threshold=cluster_similarity_threshold
        )
        
        # Initialize cross-encoder reranker
        self.use_reranker = use_reranker
        self.reranker_model = reranker_model
        self.rerank_top_k = rerank_top_k
        self.rerank_top_k_final = rerank_top_k_final
        
        # Initialize cross-encoder reranker
        self._reranker = None
        if use_reranker and DENSE_EMBEDDINGS_AVAILABLE:
            try:
                self._reranker = CrossEncoderReranker(model_name=reranker_model)
                logger.info("Cross-encoder reranker enabled")
            except Exception as e:
                logger.warning(f"Failed to initialize cross-encoder reranker: {e}")
                self._reranker = None
    
    def search(self, query: str, limit: int = 20, offset: int = 0,
               category: Optional[str] = None, tags: Optional[List[str]] = None,
               sort_by: str = "relevance") -> Dict[str, Any]:
        """
        Hybrid search combining FTS5 BM25 and dense vector search with RRF,
        plus optional cross-encoder reranking for improved precision.
        
        Pipeline:
        1. Query expansion (synonyms, entity extraction)
        2. Spell correction (fuzzy matching)
        3. Get BM25 results from FTS5
        4. Get dense vector results from FAISS
        5. Fuse with Reciprocal Rank Fusion (RRF)
        5. Optional: Cross-encoder reranking for top-k
        6. Apply MMR diversity filtering
        6. Apply freshness decay
        7. Apply result clustering (Phase 4)
        7. Apply filters (category, tags)
        7. Sort, paginate, return note objects
        
        RRF Formula: score = sum(weight_i / (rank_i + k))
        
        Returns:
            Dict with keys: 'query', 'corrected_query', 'corrections', 'count', 'offset', 'sort_by', 'results'
        """
        # 1. Query expansion and correction
        expanded_queries = [query]
        spell_corrections = []
        
        if self.use_synonym_expansion:
            expanded_queries = self.query_expander.expand(query)
            expanded_queries.extend(expanded_queries)
        
        if self.use_fuzzy_matching:
            corrected_query, corrections = self.spell_corrector.correct_query(query)
            if corrected_query != query:
                expanded_queries.append(corrected_query)
                spell_corrections = corrections
        
        # Use the best query for search (original + expanded)
        all_queries = list(set(expanded_queries))
        primary_query = expanded_queries[0] if expanded_queries else query
        
        # 1. Get BM25 results from FTS5
        bm25_results = self._search_bm25(primary_query, limit=100)  # Get more for fusion
        
        # 2. Get dense vector results
        dense_results = self._search_dense(primary_query, limit=100)
        
        # 3. Reciprocal Rank Fusion
        fused_scores = self._rrf_fusion(bm25_results, dense_results)
        
        # 4. Convert to candidate list for reranking
        candidates = [(slug, score) for slug, score in fused_scores.items()]
        candidates.sort(key=lambda x: -x[1])
        
        # 5. Optional: Cross-encoder reranking
        if self._reranker and candidates:
            logger.debug(f"Reranking top {self.rerank_top_k} candidates with cross-encoder")
            candidates = candidates[:self.rerank_top_k]
            candidates = self._reranker.rerank(
                query=primary_query,
                candidates=candidates,
                top_k=self.rerank_top_k_final
            )
        
        # Apply MMR diversity filtering
        if self.use_mmr_diversity and candidates:
            # Get embeddings for MMR
            candidate_slugs = [c[0] for c in candidates]
            candidate_embeddings = {}
            for slug in candidate_slugs:
                note = self.vault_index.get_note(slug)
                if note and _emb_present(note.embedding):
                    candidate_embeddings[slug] = note.embedding
            
            # Convert candidates to (slug, score, embedding) format
            candidates_with_emb = []
            for slug, score in candidates:
                if slug in candidate_embeddings:
                    candidates_with_emb.append((slug, score, candidate_embeddings[slug]))
            
            if candidates_with_emb:
                candidates = self.mmr_diversity.select(candidates_with_emb, 
                                                        k=self.rerank_top_k_final,
                                                        query_vec=self.embedding_pipeline.encode_single(query))
            else:
                candidates = candidates[:self.rerank_top_k_final]
        
        # Apply freshness decay
        if self.use_freshness_decay and candidates:
            # Convert to (slug, score, timestamp) format
            candidates_with_time = []
            for slug, score in candidates:
                note = self.vault_index.get_note(slug)
                if note:
                    candidates_with_time.append((slug, score, note.last_modified))
                else:
                    candidates_with_time.append((slug, score, time.time()))
            
            decayed_results = self.freshness_decay.apply_to_results(candidates_with_time)
            candidates = [(slug, score) for slug, score in decayed_results]
        
        # Apply result clustering (Phase 4)
        if self.use_result_clustering and candidates:
            # Convert to (slug, score, embedding) format for clustering
            candidates_with_emb = []
            for slug, score in candidates:
                note = self.vault_index.get_note(slug)
                if note and _emb_present(note.embedding):
                    candidates_with_emb.append((slug, score, note.embedding))
            
            if candidates_with_emb:
                clusters = self.result_clustering.cluster(candidates_with_emb)
                # Get representative from each cluster, then drop embeddings back to
                # (slug, score) for the downstream filter/sort/paginate steps.
                representatives = self.result_clustering.get_cluster_representatives(clusters)
                candidates = [(slug, score) for slug, score, _emb in representatives]
        
        # Apply filters
        filtered = self._apply_filters(dict(candidates), category, tags)
        
        # Sort and paginate
        candidates = list(filtered.items())
        candidates.sort(key=lambda x: -x[1])
        paginated = candidates[offset:offset+limit]
        
        # Fetch full note objects
        notes = []
        for slug, score in paginated:
            note = self.vault_index.get_note(slug)
            if note:
                notes.append((note, score))
        
        # Build response with spell corrections
        response = {
            "query": query,
            "corrected_query": corrected_query if 'corrected_query' in locals() else query,
            "corrections": spell_corrections if spell_corrections else [],
            "count": len(notes),
            "offset": offset,
            "sort_by": sort_by,
            "results": [
                {
                    "slug": note.slug,
                    "title": note.title,
                    "category": note.category,
                    "tags": note.tags,
                    "path": str(note.path),
                    "snippet": note.body[:200].strip(),
                }
                for note, _score in notes
            ],
        }
        
        return response
    
    def _search_bm25(self, query: str, limit: int = 100) -> List[Tuple[str, float]]:
        """Search using FTS5 BM25."""
        # Use existing FTS5 search via vault_index
        results = self.vault_index.search(query, limit=limit)
        return [(note.slug, 1.0) for note in results]  # Simplified score, will be overridden by RRF
    
    def _search_dense(self, query: str, limit: int = 100) -> List[Tuple[str, float]]:
        """Search using dense vector embeddings."""
        return self.embedding_pipeline.search(query, k=limit)
    
    def _rrf_fusion(self, bm25_results: List[Tuple[str, float]], 
                    dense_results: List[Tuple[str, float]]) -> Dict[str, float]:
        """Reciprocal Rank Fusion.
        
        score = sum(weight_i / (rank_i + k))
        """
        scores = {}
        k = self.rrf_k
        
        # BM25 ranks
        for rank, (slug, score) in enumerate(bm25_results):
            if slug not in scores:
                scores[slug] = 0
            scores[slug] += self.bm25_weight / (rank + 1 + k)
        
        # Dense ranks
        for rank, (slug, score) in enumerate(dense_results):
            if slug not in scores:
                scores[slug] = 0
            scores[slug] += self.dense_weight / (rank + 1 + k)
        
        return scores
    
    def _apply_filters(self, scores: Dict[str, float], 
                       category: Optional[str], tags: Optional[List[str]]) -> Dict[str, float]:
        """Apply category/tag filters to fused scores."""
        # Filter by checking note metadata
        # This is a simplified version - in production, you'd want to 
        # pre-filter candidates before scoring
        return scores
    
    def _check_and_refresh(self, vault_path: Path) -> bool:
        """Check if vault has changed since last scan and trigger incremental scan if needed.
        
        Returns True if index was refreshed, False if no changes detected.
        """
        if not self._vault_path:
            return False
        
        try:
            # Quick check: compare vault directory mtime with last_scan
            vault_mtime = vault_path.stat().st_mtime
            if self._last_scan and vault_mtime <= self._last_scan:
                # Directory mtime unchanged, but individual files might have been deleted
                # Check a few known files' mtimes as a lightweight check
                if self._file_mtimes:
                    for rel_path, mtime in list(self._file_mtimes.items())[:5]:  # Sample check
                        full_path = vault_path / rel_path
                        if full_path.exists() and full_path.stat().st_mtime > mtime:
                            break
                    else:
                        # Check if any tracked file was deleted
                        for rel_path in list(self._file_mtimes.keys())[:5]:
                            full_path = vault_path / rel_path
                            if not full_path.exists():
                                break
                        else:
                            return False  # No changes detected
            
            # Trigger incremental scan to pick up new/changed/deleted files
            logger.info("Vault changed since last scan, triggering incremental re-scan")
            self._incremental_scan(vault_path, max_notes=self._max_notes)
            return True
        except Exception as e:
            logger.warning(f"Failed to check/refresh vault: {e}")
            return False
"""Bounded role metadata and local FAISS search; never index full personas."""
from __future__ import annotations

import hashlib
import json
import math
import re
import tempfile
import time
from pathlib import Path

import httpx
import yaml
from filelock import FileLock

from ..config import ConfigError
from ..studio import redact


def runtime_dir(workspace):
    workspace = Path(workspace).resolve()
    path = (workspace / '.nexus' / 'orchestration').resolve()
    if not path.is_relative_to(workspace):
        raise ConfigError('Orchestration state must remain inside the workspace')
    path.mkdir(parents=True, exist_ok=True)
    return path


def plan_dir(workspace, ident):
    if not isinstance(ident, str) or not re.fullmatch(r'[a-f0-9]{32}', ident):
        raise ConfigError('Invalid project plan ID')
    root = runtime_dir(workspace)
    path = (root / 'plans' / ident).resolve()
    if not path.is_relative_to(root):
        raise ConfigError('Project plan storage must remain inside the workspace')
    path.mkdir(parents=True, exist_ok=True)
    return path


def atomic_json(path, data):
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as file:
            temporary = Path(file.name)
            file.write(json.dumps(redact(data), ensure_ascii=False, indent=2, allow_nan=False))
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def tokens(text):
    return re.findall(r'[\w]+', text.casefold())[:10000]


def metadata(config):
    records = []
    for role in sorted(config.roles.roles.values(), key=lambda role: role.name):
        # Existing YAML loader remains authoritative. Personas never leave it here.
        description = ' '.join((role.persona or role.title or role.name).split())[:200]
        records.append(redact({'id': role.name, 'name': (role.title or role.name)[:120],
            'category': role.category[:80], 'tags': [k.text[:80] for k in role.keywords[:20]],
            'description': description, 'skills': [s[:100] for s in role.focus[:12]], 'tools_needed': [t[:64] for t in (role.tools or [])[:20]]}))
    if not 1 <= len(records) <= 10000:
        raise ConfigError('Role index supports 1–10,000 roles')
    if len(json.dumps(records).encode()) > 7_000_000:
        raise ConfigError('Role metadata exceeds the bounded 7 MB index budget')
    return records


def settings(workspace, path=None):
    path = Path(path) if path else Path(workspace) / 'role-index.yaml'
    data = yaml.safe_load(path.read_text(encoding='utf-8')) if path.exists() else {}
    if not isinstance(data, dict) or set(data) - {'encoder', 'model', 'base_url', 'dimensions', 'confidence', 'simple_agent', 'complex_agent'}:
        raise ConfigError('Invalid role-index.yaml settings')
    data = {'encoder': 'ollama', 'model': 'nomic-embed-text', 'base_url': 'http://127.0.0.1:11434',
            'dimensions': 384, 'confidence': .65, 'simple_agent': '', 'complex_agent': '', **data}
    if data['encoder'] not in {'ollama', 'lexical-hash'}:
        raise ConfigError('Encoder must be ollama or lexical-hash')
    if type(data['dimensions']) is not int or not 32 <= data['dimensions'] <= 8192:
        raise ConfigError('Embedding dimensions must be 32–8192')
    if type(data['confidence']) not in {int, float} or not math.isfinite(data['confidence']) or not 0 <= data['confidence'] <= 1:
        raise ConfigError('Confidence must be a finite number in 0..1')
    if any(not isinstance(data[k], str) for k in ('model', 'base_url', 'simple_agent', 'complex_agent')):
        raise ConfigError('Model/URL/agent names must be strings')
    if data['encoder'] == 'ollama':
        url = httpx.URL(data['base_url'])
        if url.scheme not in {'http', 'https'} or url.host not in {'localhost', '127.0.0.1', '::1'} or url.username or url.password or url.query or url.fragment:
            raise ConfigError('Role embeddings must use a credential-free loopback Ollama URL')
    return data


class RoleIndex:
    def __init__(self, config, workspace, options=None, *, embedder=None):
        try:
            import faiss
            import numpy as np
        except ImportError as exc:
            raise ConfigError('Install vector support: pip install -e ".[orchestrator]"') from exc
        self.faiss, self.np = faiss, np
        self.faiss.omp_set_num_threads(1)
        self.config, self.workspace = config, Path(workspace).resolve()
        self.options = settings(workspace) if options is None else options
        self.records = metadata(config)
        self.root = runtime_dir(workspace)
        self.embedder = embedder or self.embed
        self.signature = hashlib.sha256(json.dumps([self.records, self.options], sort_keys=True).encode()).hexdigest()
        self.matrix = None
        self.vector = None

    def embed(self, texts):
        if self.options['encoder'] == 'lexical-hash':
            result = self.np.zeros((len(texts), self.options['dimensions']), dtype='float32')
            for row, text in enumerate(texts):
                for term in tokens(text):
                    digest = hashlib.sha256(term.encode()).digest()
                    result[row, int.from_bytes(digest[:4], 'little') % result.shape[1]] += 1 if digest[4] & 1 else -1
            return result
        vectors = []
        for start in range(0, len(texts), 32):
            response = httpx.post(self.options['base_url'].rstrip('/') + '/api/embed', timeout=60,
                                  json={'model': self.options['model'], 'input': texts[start:start + 32]})
            response.raise_for_status()
            batch = response.json().get('embeddings')
            if not isinstance(batch, list) or len(batch) != len(texts[start:start + 32]):
                raise ConfigError('Ollama returned an incomplete embedding batch')
            vectors.extend(batch)
        return vectors

    def normalize(self, vectors, count):
        values = self.np.asarray(vectors, dtype='float32')
        if values.ndim != 2 or values.shape[0] != count or not 1 <= values.shape[1] <= 8192 or values.nbytes > 64_000_000:
            raise ConfigError('Invalid or oversized embedding matrix')
        if not self.np.isfinite(values).all():
            raise ConfigError('Embedding vectors must be finite')
        norms = self.np.linalg.norm(values, axis=1)
        if not self.np.isfinite(norms).all() or (norms <= 0).any():
            raise ConfigError('Embedding vectors must not be empty/zero')
        return self.np.ascontiguousarray(values / norms[:, None])

    def ensure(self, force=False):
        with FileLock(str(self.root / 'index.lock'), timeout=120):
            manifest = self.root / 'index.json'
            vectors_path = self.root / 'vectors.npy'
            if manifest.is_symlink() or vectors_path.is_symlink():
                raise ConfigError('Role cache files must not be symlinks')
            cached = False
            if not force and manifest.exists() and vectors_path.exists():
                if manifest.stat().st_size > 8_000_000 or vectors_path.stat().st_size > 64_000_000:
                    raise ConfigError('Oversized role index cache; rebuild explicitly')
                prior = json.loads(manifest.read_text(encoding='utf-8'))
                if prior.get('signature') == self.signature:
                    if hashlib.sha256(vectors_path.read_bytes()).hexdigest() != prior.get('vectors_sha256'):
                        raise ConfigError('Corrupt role vectors; rebuild with --force')
                    self.matrix = self.normalize(self.np.load(vectors_path, allow_pickle=False), len(self.records))
                    cached = True
            if not cached:
                texts = [json.dumps(record, ensure_ascii=False) for record in self.records]
                self.matrix = self.normalize(self.embedder(texts), len(texts))
                with tempfile.NamedTemporaryFile(dir=self.root, delete=False) as file:
                    temporary = Path(file.name)
                    self.np.save(file, self.matrix, allow_pickle=False)
                try:
                    temporary.replace(vectors_path)
                finally:
                    temporary.unlink(missing_ok=True)
                atomic_json(manifest, {'schema': 1, 'signature': self.signature,
                    'vectors_sha256': hashlib.sha256(vectors_path.read_bytes()).hexdigest(),
                    'roles': self.records, 'encoder': self.options['encoder'], 'model': self.options['model'], 'indexed_at': time.time()})
        self.vector = self.faiss.IndexFlatIP(self.matrix.shape[1])
        self.vector.add(self.matrix)
        return {'roles': len(self.records), 'cached': cached, 'encoder': self.options['encoder'], 'signature': self.signature}

    def search(self, task, limit=15, quality=None, allowed=None):
        if not isinstance(task, str) or not task.strip() or len(task) > 20000:
            raise ConfigError('A task of 1–20,000 characters is required')
        if self.vector is None:
            self.ensure()
        task = redact(task)
        query = self.normalize(self.embedder([task]), 1)
        if query.shape[1] != self.matrix.shape[1]:
            raise ConfigError('Embedding model dimensions changed; rebuild the role index')
        similarities, indices = self.vector.search(query, len(self.records))
        semantic = {int(i): max(0., float(s)) for i, s in zip(indices[0], similarities[0], strict=True)}
        terms = set(tokens(task))
        scored = []
        for index, record in enumerate(self.records):
            if allowed is not None and record['id'] not in allowed:
                continue
            words = set(tokens(json.dumps(record, ensure_ascii=False)))
            lexical = len(terms & words) / max(1, len(terms))
            prior = (quality or {}).get(record['id'], {'score': .5, 'observations': 0})
            history = min(1., max(0., float(prior['score'])))
            exploration = .01 / (1 + max(0, int(prior['observations'])))
            score = .55 * semantic[index] + .4 * lexical + .05 * history + exploration
            scored.append({**record, 'score': round(score, 6), 'embedding_score': semantic[index], 'keyword_score': lexical,
                           'quality_score': history, 'quality_observations': prior['observations'], 'exploration_bonus': exploration,
                           'score_basis': '0.55 semantic + 0.4 lexical + 0.05 observed quality + bounded exploration'})
        return sorted(scored, key=lambda row: (-row['score'], row['id']))[:max(1, min(int(limit), 15))]

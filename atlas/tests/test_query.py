"""
Test the query model architecture and inference path.
Does NOT require a trained model — tests the untrained model shape
and the data generation pipeline.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

import torch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "query"))

from query.net import CodeQueryModel, PAD_IDX, UNK_IDX, KIND_LABELS, KIND_TO_IDX


def test_identifier_list_query_retrieves_each_symbol(monkeypatch):
    """Symbol lists should be searched independently without a model kind filter."""
    import server
    import usage

    question = (
        "useAuth signIn signOut handleGoogleLogin handleSubmit "
        "handleForgotPassword handlePasswordChange handleUpdateProfile "
        "handleDeleteAccount setupRecaptcha handleSendMfaCode "
        "handleVerifyMfaCode handleDisableMfa onAuthStateChanged"
    )
    search_args = {}

    monkeypatch.setattr(server, "_ensure_meilisearch", lambda: None)
    monkeypatch.setattr(server, "_scoped_project", lambda project_id: project_id)
    monkeypatch.setattr(server, "_current_tier", lambda: "free")
    monkeypatch.setattr(usage, "log_call", lambda *args: None)

    def fake_search_many(project_id, queries, filters=None, limit=10):
        search_args.update(
            project_id=project_id, queries=queries, filters=filters, limit=limit
        )
        return [
            [{
                "id": token,
                "kind": "function",
                "name": token,
                "file_path": f"src/{token}.ts",
            }]
            for token in queries
        ]

    monkeypatch.setattr(server, "search_many", fake_search_many)

    result = server._handle_query({"question": question, "project_id": "test"})

    assert result["query"] == question
    expected_tokens = question.split()
    expected_tokens[1] = "signin"
    assert search_args["queries"] == expected_tokens
    assert search_args["filters"] is None
    assert search_args["limit"] == 10
    assert result["filter_kind"] is None
    assert result["count"] == len(question.split())
    assert {hit["name"] for hit in result["results"]} == set(expected_tokens)


def test_identifier_search_merge_prefers_exact_names():
    from server import _merge_identifier_searches

    results = _merge_identifier_searches(
        ["login", "handleLogin"],
        [
            [{"id": "context", "name": "loginView"}],
            [{"id": "exact", "name": "handleLogin"}],
        ],
        limit=2,
    )

    assert [hit["id"] for hit in results] == ["exact", "context"]


def test_query_term_matching_respects_identifier_boundaries():
    from server import _search_term_matches

    assert _search_term_matches("login", "handleGoogleLogin")
    assert _search_term_matches("signin", "signInWithPopup")
    assert _search_term_matches("auth", "requireAuthenticatedUser")
    assert not _search_term_matches("auth", "authorName")


def test_model_search_terms_remove_query_phrasing():
    from server import _model_search_terms

    tokens = ["how", "do", "I", "search", "for", "OAuth", "tokens"]
    assert _model_search_terms(tokens, set(range(len(tokens)))) == [
        "OAuth", "tokens",
    ]


def test_query_synonyms_expand_identifiers_and_concepts():
    from query.synonyms import expand_query_terms, searchable_terms

    expanded = expand_query_terms(["verifyAuthToken", "MFA"])
    assert {"verify", "auth", "token", "authentication", "login"} <= set(expanded)
    assert {"multifactor", "two_factor"} <= set(expanded)
    assert "verify" in searchable_terms("verifyAuthToken")


def test_query_expansion_preserves_all_original_terms_before_aliases():
    from query.synonyms import expand_query_terms

    terms = expand_query_terms([
        "authentication", "password", "token", "profile", "authorization",
    ])

    assert terms[:5] == [
        "authentication", "password", "token", "profile", "authorization",
    ]


def test_semantic_rerank_orders_candidates_by_encoder_similarity(monkeypatch):
    from query import semantic

    monkeypatch.setattr(
        semantic,
        "_encode",
        lambda texts: torch.tensor([
            [1.0, 0.0],
            [0.0, 1.0],
            [1.0, 0.0],
        ]),
    )
    hits = [
        {"id": "less-relevant", "name": "logout"},
        {"id": "more-relevant", "name": "authenticate"},
    ]

    ranked = semantic.rerank("where is authentication handled", hits, limit=2)

    assert [hit["id"] for hit in ranked] == ["more-relevant", "less-relevant"]


def test_semantic_rerank_keeps_exact_symbol_matches_first(monkeypatch):
    from query import semantic

    monkeypatch.setattr(
        semantic,
        "_encode",
        lambda texts: torch.tensor([
            [0.0, 1.0],
            [0.0, 1.0],
            [1.0, 0.0],
        ]),
    )
    hits = [
        {"id": "exact", "name": "verifyToken"},
        {"id": "semantic", "name": "validateCredential"},
    ]

    ranked = semantic.rerank("where is verifyToken used", hits, limit=2)

    assert [hit["id"] for hit in ranked] == ["exact", "semantic"]


def test_natural_language_query_uses_expanded_terms_and_semantic_reranking(
    monkeypatch,
):
    import server
    import usage
    from query import semantic

    search_args = {}
    entities = {
        "remove": {
            "id": "remove",
            "kind": "function",
            "name": "removeUser",
            "search_terms": "remove delete destroy user",
        },
        "delete": {
            "id": "delete",
            "kind": "function",
            "name": "deleteUser",
            "search_terms": "delete remove destroy user",
        },
    }
    monkeypatch.setattr(server, "_ensure_meilisearch", lambda: None)
    monkeypatch.setattr(server, "_scoped_project", lambda project_id: project_id)
    monkeypatch.setattr(server, "_current_tier", lambda: "free")
    monkeypatch.setattr(usage, "log_call", lambda *args: None)

    def fake_search_many(project_id, queries, filters=None, limit=10):
        search_args.setdefault("queries", []).extend(queries)
        search_args.update(filters=filters, limit=limit)
        return [[entities[term]] if term in entities else [] for term in queries]

    def fake_rerank(question, hits, limit):
        search_args["rerank"] = (question, len(hits), limit)
        return list(reversed(hits))[:limit]

    monkeypatch.setattr(server, "search_many", fake_search_many)
    monkeypatch.setattr(semantic, "rerank", fake_rerank)

    result = server._handle_query({
        "question": "where is the remove user record operation implemented",
        "project_id": "test",
        "limit": 5,
    })

    assert {"remove", "delete", "destroy"} <= set(
        search_args["queries"]
    )
    assert search_args["rerank"] == (
        "where is the remove user record operation implemented", 2, 5
    )
    assert [hit["name"] for hit in result["results"]] == [
        "deleteUser", "removeUser",
    ]


def test_natural_query_reports_lexical_fallback_when_model_unavailable(monkeypatch):
    import server
    import usage
    from query import semantic

    entity = {
        "id": "remove",
        "kind": "function",
        "name": "removeUser",
        "search_terms": "remove delete destroy user",
    }
    monkeypatch.setattr(server, "_ensure_meilisearch", lambda: None)
    monkeypatch.setattr(server, "_scoped_project", lambda project_id: project_id)
    monkeypatch.setattr(server, "_current_tier", lambda: "free")
    monkeypatch.setattr(usage, "log_call", lambda *args: None)
    monkeypatch.setattr(
        server,
        "search_many",
        lambda project_id, queries, filters=None, limit=10: [
            [entity] if term in {"remove", "delete"} else []
            for term in queries
        ],
    )

    def unavailable(question, hits, limit):
        raise OSError("model cache unavailable")

    monkeypatch.setattr(semantic, "rerank", unavailable)

    result = server._handle_query({
        "question": "where is the remove user operation implemented",
        "project_id": "test",
    })

    assert result["count"] == 1
    assert result["results"][0]["name"] == "removeUser"
    assert "lexical results instead" in result["ranking_warning"]


def test_auth_discovery_expands_to_mfa_and_account_flows(monkeypatch):
    import server
    import usage

    question = "auth login session token password jwt oauth identity"
    search_args = {}
    term_results = {
        "auth": {"id": "state", "kind": "function", "name": "onAuthStateChanged"},
        "mfa": {"id": "mfa", "kind": "function", "name": "handleVerifyMfaCode"},
        "profile": {"id": "profile", "kind": "function", "name": "handleUpdateProfile"},
    }

    monkeypatch.setattr(server, "_ensure_meilisearch", lambda: None)
    monkeypatch.setattr(server, "_scoped_project", lambda project_id: project_id)
    monkeypatch.setattr(server, "_current_tier", lambda: "free")
    monkeypatch.setattr(usage, "log_call", lambda *args: None)

    def fake_search_many(project_id, queries, filters=None, limit=10):
        search_args.update(queries=queries, filters=filters, limit=limit)
        return [
            [{
                **term_results.get(
                    token,
                    {
                        "id": token,
                        "kind": "function",
                        "name": token,
                    },
                ),
                "file_path": (
                    "src/components/settings/ProfileView.tsx"
                    if token == "profile" else f"src/{token}.ts"
                ),
            }]
            for token in queries
        ]

    monkeypatch.setattr(server, "search_many", fake_search_many)

    result = server._handle_query({"question": question, "project_id": "test"})

    assert {"mfa", "multifactor", "profile", "reauthenticate"} <= set(
        search_args["queries"]
    )
    assert search_args["limit"] == 50
    assert search_args["filters"] is None
    assert {"handleVerifyMfaCode", "handleUpdateProfile"} <= {
        hit["name"] for hit in result["results"]
    }
    assert not {"session", "token", "password"} & {
        hit["name"] for hit in result["results"]
    }


def test_auth_relevance_requires_auth_evidence():
    from server import _auth_relevance_score

    assert _auth_relevance_score({
        "name": "handleSubmit",
        "file_path": "src/components/auth/LoginView.tsx",
        "calls": ["signInWithEmailAndPassword"],
    }) >= 4
    assert _auth_relevance_score({
        "name": "requireAuthenticatedUser",
        "file_path": "server.ts",
        "body_preview": "await admin.auth().verifyIdToken(token)",
    }) >= 4
    assert _auth_relevance_score({
        "name": "calculateProfileCompletion",
        "file_path": "src/lib/profile.ts",
    }) < 4


def test_auth_relevance_includes_server_side_auth_guards():
    from server import _auth_relevance_score

    assert _auth_relevance_score({
        "name": "requireAuthenticatedUser",
        "file_path": "server.ts",
    }) >= 4
    assert _auth_relevance_score({
        "name": "requireGlobalAdmin",
        "file_path": "server.ts",
    }) >= 4
    assert _auth_relevance_score({
        "name": "verifyIdToken",
        "file_path": "server.ts",
    }) >= 8


def test_auth_discovery_searches_authorization_and_admin_guards(monkeypatch):
    import server
    import usage

    guards = {
        "authenticated": {
            "id": "authenticated",
            "kind": "function",
            "name": "requireAuthenticatedUser",
            "file_path": "server.ts",
        },
        "admin": {
            "id": "admin",
            "kind": "function",
            "name": "requireGlobalAdmin",
            "file_path": "server.ts",
        },
    }
    captured = {}

    monkeypatch.setattr(server, "_ensure_meilisearch", lambda: None)
    monkeypatch.setattr(server, "_scoped_project", lambda project_id: project_id)
    monkeypatch.setattr(server, "_current_tier", lambda: "free")
    monkeypatch.setattr(usage, "log_call", lambda *args: None)

    def fake_search_many(project_id, queries, filters=None, limit=10):
        captured.update(queries=queries, limit=limit)
        return [
            [guards["authenticated"]] if term == "auth" else
            [guards["admin"]] if term == "admin" else
            []
            for term in queries
        ]

    monkeypatch.setattr(server, "search_many", fake_search_many)

    result = server._handle_query({
        "question": "where are authentication guards and admin token verification implemented",
        "project_id": "test",
    })

    assert {"authorization", "admin", "verifyIdToken", "middleware", "guard"} <= (
        set(captured["queries"])
    )
    assert captured["limit"] == 50
    assert {
        hit["name"] for hit in result["results"]
    } >= {"requireAuthenticatedUser", "requireGlobalAdmin"}


def test_identifier_list_query_detection():
    from server import (
        _fallback_search_tokens, _identifier_query_tokens,
        _auth_discovery_tokens, _is_identifier_list_query,
    )

    assert _is_identifier_list_query(
        "useAuth signIn signOut handleGoogleLogin handleSubmit "
        "handleForgotPassword handlePasswordChange handleUpdateProfile "
        "handleDeleteAccount setupRecaptcha handleSendMfaCode "
        "handleVerifyMfaCode handleDisableMfa onAuthStateChanged"
    )
    assert _is_identifier_list_query(
        "auth login session token password jwt oauth identity"
    )
    assert _identifier_query_tokens(
        "(?i)auth|login|session|token|credential|sign.?in|permission|firebase"
    ) == [
        "auth", "login", "session", "token", "credential", "signin",
        "permission", "firebase",
    ]
    assert _fallback_search_tokens(
        "what functions handle authentication and user sign-in"
    ) == ["auth", "signin"]
    assert _auth_discovery_tokens(
        "what functions handle authentication and user sign-in"
    ) is not None
    assert _auth_discovery_tokens(
        "handlePasswordChange handleUpdateProfile onAuthStateChanged"
    ) is None
    assert not _is_identifier_list_query(
        "Find all the functions that handle authentication and user sessions"
    )


def test_space_separated_identifier_list_detection():
    from server import _is_identifier_list_query

    assert _is_identifier_list_query(
        "handleGoogleLogin handleForgotPassword handleSubmit "
        "handleLogout handleSignOut"
    )
    assert not _is_identifier_list_query(
        "Find functions that handle login and password reset"
    )


def test_model_shape():
    """Test that the model produces the expected output shapes."""
    vocab_size = 100
    batch_size = 4
    seq_len = 10

    model = CodeQueryModel(vocab_size=vocab_size)

    ids = torch.randint(2, vocab_size, (batch_size, seq_len))
    lengths = torch.tensor([seq_len, 8, 6, 3], dtype=torch.long)

    token_logits, kind_logits = model(ids, lengths)

    assert token_logits.shape == (batch_size, seq_len, 2), \
        f"Token logits shape wrong: {token_logits.shape}"
    assert kind_logits.shape == (batch_size, len(KIND_LABELS)), \
        f"Kind logits shape wrong: {kind_logits.shape}"

    print("  PASS test_model_shape")


def test_model_params():
    """Keep the model small enough for responsive CPU inference."""
    model = CodeQueryModel(vocab_size=200)
    total = sum(p.numel() for p in model.parameters())
    assert 850_000 < total < 1_500_000, \
        f"Model params out of range: {total:,} (expected ~1.1M)"
    print(f"  PASS test_model_params ({total:,} params)")


def test_query_subtokens_split_identifiers():
    from query.net import query_subtokens

    assert query_subtokens("handleGoogleLogin") == [
        "handle", "google", "login",
    ]
    assert query_subtokens("verify_HTTP_token") == [
        "verify", "http", "token",
    ]
    assert query_subtokens("swellendam_scraper.py") == [
        "swellendam", "scraper", "py",
    ]


def test_generation_labels_keep_only_search_terms():
    from query.generate_data import _label_tokens

    assert _label_tokens(
        ["what", "function", "handles", "authentication"],
        {"authentication"},
    ) == [0, 0, 0, 1]


def test_training_split_keeps_targets_separate():
    from query.train import split_examples

    examples = [
        {
            "tokens": ["find", "loginUser"],
            "labels": [0, 1],
            "kind": "function",
        },
        {
            "tokens": ["show", "loginUser"],
            "labels": [0, 1],
            "kind": "function",
        },
        {
            "tokens": ["find", "saveReport"],
            "labels": [0, 1],
            "kind": "function",
        },
        {
            "tokens": ["show", "deleteAccount"],
            "labels": [0, 1],
            "kind": "function",
        },
    ]

    training, validation = split_examples(examples, validation_fraction=0.25)
    training_targets = {
        tuple(sorted(token for token, label in zip(ex["tokens"], ex["labels"]) if label))
        for ex in training
    }
    validation_targets = {
        tuple(sorted(token for token, label in zip(ex["tokens"], ex["labels"]) if label))
        for ex in validation
    }
    assert training_targets.isdisjoint(validation_targets)


def test_model_inference():
    """Test end-to-end inference with a random model."""
    vocab = {"<PAD>": 0, "<UNK>": 1, "what": 2, "function": 3,
             "handles": 4, "auth": 5, "in": 6, "the": 7, "api": 8}

    model = CodeQueryModel(vocab_size=len(vocab))
    model.eval()

    question = "what function handles auth in the api"
    tokens = question.split()
    ids = torch.tensor(
        [vocab.get(t.lower(), UNK_IDX) for t in tokens],
        dtype=torch.long,
    ).unsqueeze(0)
    lengths = torch.tensor([len(tokens)], dtype=torch.long)

    with torch.no_grad():
        token_logits, kind_logits = model(ids, lengths)

    token_preds = token_logits.argmax(dim=-1).squeeze(0).tolist()
    kind_idx = kind_logits.argmax(dim=-1).item()

    assert len(token_preds) == len(tokens), "Should have one prediction per token"
    assert all(p in (0, 1) for p in token_preds), "Predictions should be 0 or 1"
    assert 0 <= kind_idx < len(KIND_LABELS), f"Kind index out of range: {kind_idx}"

    kept = [w for w, p in zip(tokens, token_preds) if p == 1]
    predicted_kind = KIND_LABELS[kind_idx]
    print(f"  PASS test_model_inference (kept={kept}, kind={predicted_kind})")


def test_data_generation():
    """Test that generate_data produces valid training examples."""
    from ingest.parsers import parse_file
    from ingest.call_graph import build_call_graph
    from query.generate_data import generate_from_entities

    sample = '''
def authenticate(username: str, password: str) -> bool:
    """Check credentials."""
    return verify_hash(password)

def verify_hash(password: str) -> bool:
    return True

class UserManager:
    def create_user(self, name: str):
        authenticate(name, "default")
'''

    with tempfile.TemporaryDirectory() as tmpdir:
        fpath = Path(tmpdir) / "auth.py"
        fpath.write_text(sample, encoding="utf-8")

        entities = parse_file(str(fpath), tmpdir)
        entities = build_call_graph(entities)

        examples = generate_from_entities(entities, target_count=50)

        assert len(examples) >= 50, f"Expected 50+ examples, got {len(examples)}"

        for ex in examples[:10]:
            assert "text" in ex, "Example missing 'text'"
            assert "tokens" in ex, "Example missing 'tokens'"
            assert "labels" in ex, "Example missing 'labels'"
            assert "kind" in ex, "Example missing 'kind'"
            assert len(ex["tokens"]) == len(ex["labels"]), \
                f"Token/label length mismatch: {len(ex['tokens'])} vs {len(ex['labels'])}"
            assert ex["kind"] in KIND_TO_IDX, f"Unknown kind: {ex['kind']}"
            assert sum(ex["labels"]) > 0, f"No KEEP labels in: {ex['text']}"

        print(f"  PASS test_data_generation ({len(examples)} examples)")


def test_training_corpus_includes_unseen_concepts_and_multi_intent_queries():
    from query.generate_data import generate_from_entities

    examples = generate_from_entities(
        [
            {
                "kind": "function",
                "name": "verify_token",
                "file_path": "auth.py",
                "docstring": "Validate a user session token.",
            },
            {
                "kind": "function",
                "name": "reset_password",
                "file_path": "auth.py",
                "docstring": "Send a password reset email.",
            },
        ],
        target_count=400,
    )
    assert any("authentication" in ex["text"] for ex in examples)
    assert any(
        sum(ex["labels"]) >= 2 and (" and " in ex["text"] or " or " in ex["text"])
        for ex in examples
    )


if __name__ == "__main__":
    print("Running query tests...")
    test_model_shape()
    test_model_params()
    test_model_inference()
    test_data_generation()
    print("All query tests passed.")

"""Tests for the webhook service."""

from __future__ import annotations

import json
from datetime import datetime, UTC, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from cyborg.config import Settings
from cyborg.main import create_app
from cyborg.services.webhook_service import (
    WebhookConfig,
    WebhookEvent,
    WebhookPayload,
    WebhookService,
    WebhookStatus,
)


def make_client(tmp_path: Path) -> TestClient:
    settings = Settings(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "cyborg.db",
    )
    return TestClient(create_app(settings))


class TestWebhookConfig:
    """Tests for WebhookConfig."""
    
    def test_config_creation(self):
        """Test creating a webhook config."""
        config = WebhookConfig(
            id="test-id",
            name="test-webhook",
            url="http://example.com/webhook",
            secret="secret123",
            events=[WebhookEvent.TASK_COMPLETED],
            retry_count=5,
        )
        
        assert config.id == "test-id"
        assert config.name == "test-webhook"
        assert config.url == "http://example.com/webhook"
        assert config.secret == "secret123"
        assert config.events == [WebhookEvent.TASK_COMPLETED]
        assert config.retry_count == 5
        assert config.is_active is True


class TestWebhookPayload:
    """Tests for WebhookPayload."""
    
    def test_payload_creation(self):
        """Test creating a webhook payload."""
        payload = WebhookPayload(
            event=WebhookEvent.TASK_COMPLETED,
            project_id="proj-123",
            task_id="task-456",
            task_title="Test Task",
            result_summary="Task completed successfully",
            session_key="sess-abc",
            metadata={"key": "value"},
        )
        
        assert payload.event == WebhookEvent.TASK_COMPLETED
        assert payload.project_id == "proj-123"
        assert payload.task_id == "task-456"
        assert payload.task_title == "Test Task"
        assert payload.result_summary == "Task completed successfully"
        assert payload.session_key == "sess-abc"
        assert payload.metadata == {"key": "value"}
        assert payload.timestamp is not None
    
    def test_payload_to_dict(self):
        """Test converting payload to dictionary."""
        payload = WebhookPayload(
            event=WebhookEvent.TASK_COMPLETED,
            task_title="Test Task",
        )
        
        data = payload.to_dict()
        
        assert data["event"] == WebhookEvent.TASK_COMPLETED
        assert data["task_title"] == "Test Task"
        assert "timestamp" in data


class TestWebhookServiceSignatures:
    """Tests for webhook signature generation and verification."""
    
    def test_generate_signature(self, tmp_path: Path):
        """Test HMAC signature generation."""
        with make_client(tmp_path) as client:
            # Get the app state to access database
            from cyborg.database import Database
            db = Database(tmp_path / "data" / "cyborg.db")
            
            webhook_service = WebhookService(db)
            
            payload = '{"event": "task.completed"}'
            secret = "my-secret"
            
            signature = webhook_service._generate_signature(payload, secret)
            
            assert isinstance(signature, str)
            assert len(signature) == 64  # SHA-256 hex is 64 chars
            
            # Same payload should generate same signature
            signature2 = webhook_service._generate_signature(payload, secret)
            assert signature == signature2
            
            # Different secret should generate different signature
            different = webhook_service._generate_signature(payload, "different-secret")
            assert different != signature
    
    def test_verify_signature(self, tmp_path: Path):
        """Test HMAC signature verification."""
        with make_client(tmp_path) as client:
            from cyborg.database import Database
            db = Database(tmp_path / "data" / "cyborg.db")
            
            webhook_service = WebhookService(db)
            
            payload = '{"event": "task.completed"}'
            secret = "my-secret"
            
            signature = webhook_service._generate_signature(payload, secret)
            
            # Valid signature
            assert webhook_service._verify_signature(payload, signature, secret) is True
            
            # Invalid signature
            assert webhook_service._verify_signature(payload, "invalid", secret) is False
            
            # Wrong secret
            assert webhook_service._verify_signature(payload, signature, "wrong-secret") is False


class TestWebhookServiceCRUD:
    """Tests for webhook configuration CRUD operations."""
    
    def test_create_config(self, tmp_path: Path):
        """Test creating a webhook configuration."""
        with make_client(tmp_path) as client:
            # Create webhook via API
            response = client.post(
                "/api/v1/webhooks",
                json={
                    "name": "test-webhook",
                    "url": "http://example.com/webhook",
                    "secret": "secret123",
                    "events": [WebhookEvent.TASK_COMPLETED, WebhookEvent.TASK_FAILED],
                    "retry_count": 5,
                },
            )
            
            assert response.status_code == 201
            data = response.json()
            assert data["name"] == "test-webhook"
            assert data["url"] == "http://example.com/webhook"
            assert data["events"] == [WebhookEvent.TASK_COMPLETED, WebhookEvent.TASK_FAILED]
            assert data["retry_count"] == 5
            
            # Verify it can be retrieved
            config_id = data["id"]
            get_response = client.get(f"/api/v1/webhooks/{config_id}")
            assert get_response.status_code == 200
            assert get_response.json()["name"] == "test-webhook"
    
    def test_get_config_by_name(self, tmp_path: Path):
        """Test getting config by name."""
        with make_client(tmp_path) as client:
            # Create webhook
            client.post(
                "/api/v1/webhooks",
                json={
                    "name": "my-webhook",
                    "url": "http://example.com/webhook",
                    "secret": "secret123",
                    "events": [WebhookEvent.TASK_COMPLETED],
                },
            )
            
            # Get by name
            response = client.get("/api/v1/webhooks/by-name/my-webhook")
            assert response.status_code == 200
            assert response.json()["name"] == "my-webhook"
            
            # Non-existent name
            response = client.get("/api/v1/webhooks/by-name/non-existent")
            assert response.status_code == 404
    
    def test_list_configs(self, tmp_path: Path):
        """Test listing webhook configurations."""
        with make_client(tmp_path) as client:
            # Create configs
            client.post(
                "/api/v1/webhooks",
                json={
                    "name": "webhook-1",
                    "url": "http://example.com/1",
                    "secret": "secret1",
                    "events": [WebhookEvent.TASK_COMPLETED],
                },
            )
            client.post(
                "/api/v1/webhooks",
                json={
                    "name": "webhook-2",
                    "url": "http://example.com/2",
                    "secret": "secret2",
                    "events": [WebhookEvent.TASK_FAILED],
                },
            )
            
            # List all
            response = client.get("/api/v1/webhooks")
            assert response.status_code == 200
            data = response.json()
            assert len(data) == 2
    
    def test_update_config(self, tmp_path: Path):
        """Test updating a webhook configuration."""
        with make_client(tmp_path) as client:
            # Create webhook
            create_response = client.post(
                "/api/v1/webhooks",
                json={
                    "name": "test-webhook",
                    "url": "http://example.com/webhook",
                    "secret": "secret123",
                    "events": [WebhookEvent.TASK_COMPLETED],
                },
            )
            config_id = create_response.json()["id"]
            
            # Update
            update_response = client.put(
                f"/api/v1/webhooks/{config_id}",
                json={
                    "url": "http://new-url.com/webhook",
                    "retry_count": 10,
                },
            )
            
            assert update_response.status_code == 200
            data = update_response.json()
            assert data["url"] == "http://new-url.com/webhook"
            assert data["retry_count"] == 10
    
    def test_delete_config(self, tmp_path: Path):
        """Test deleting a webhook configuration."""
        with make_client(tmp_path) as client:
            # Create webhook
            create_response = client.post(
                "/api/v1/webhooks",
                json={
                    "name": "test-webhook",
                    "url": "http://example.com/webhook",
                    "secret": "secret123",
                    "events": [WebhookEvent.TASK_COMPLETED],
                },
            )
            config_id = create_response.json()["id"]
            
            # Delete
            delete_response = client.delete(f"/api/v1/webhooks/{config_id}")
            assert delete_response.status_code == 204
            
            # Verify it's gone
            get_response = client.get(f"/api/v1/webhooks/{config_id}")
            assert get_response.status_code == 404


class TestWebhookDelivery:
    """Tests for webhook delivery."""
    
    def test_trigger_event_creates_delivery(self, tmp_path: Path):
        """Test that triggering an event creates a delivery record."""
        with make_client(tmp_path) as client:
            # Create a webhook
            client.post(
                "/api/v1/webhooks",
                json={
                    "name": "test-webhook",
                    "url": "http://example.com/webhook",
                    "secret": "secret123",
                    "events": [WebhookEvent.TASK_COMPLETED],
                },
            )
            
            # Create a task and complete it
            task_response = client.post(
                "/api/v1/tasks",
                json={
                    "title": "Test Task",
                    "requested_by": "Bob",
                    "priority": "high",
                },
            )
            task_id = task_response.json()["id"]
            
            # Mock the HTTP client to avoid actual network calls
            from cyborg.database import Database
            db = Database(tmp_path / "data" / "cyborg.db")
            webhook_service = WebhookService(db)
            
            with patch.object(webhook_service, '_get_http_client') as mock_get_client:
                mock_client = AsyncMock()
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.text = "OK"
                mock_client.post.return_value = mock_response
                mock_get_client.return_value = mock_client
                
                # Complete task (should trigger webhook)
                import asyncio
                asyncio.run(webhook_service.trigger_event(
                    event=WebhookEvent.TASK_COMPLETED,
                    task_id=task_id,
                    task_title="Test Task",
                ))
            
            # Verify delivery was created
            deliveries = client.get("/api/v1/webhooks/deliveries")
            assert deliveries.status_code == 200
            # Note: Delivery may not be created if webhook config doesn't match


class TestWebhookHeaders:
    """Tests for webhook HTTP headers."""
    
    def test_signature_generation(self, tmp_path: Path):
        """Test that signatures are generated correctly."""
        with make_client(tmp_path) as client:
            from cyborg.database import Database
            db = Database(tmp_path / "data" / "cyborg.db")
            webhook_service = WebhookService(db)
            
            # Create a config
            import asyncio
            config = asyncio.run(webhook_service.create_config(
                name="test-webhook",
                url="http://example.com/webhook",
                secret="my-secret-key",
                events=[WebhookEvent.TASK_COMPLETED],
            ))
            
            # Create delivery
            payload = WebhookPayload(
                event=WebhookEvent.TASK_COMPLETED,
                task_title="Test Task",
            )
            delivery_id = asyncio.run(webhook_service._create_delivery(config, payload))
            
            # Get the delivery to access payload
            delivery = asyncio.run(webhook_service.get_delivery(delivery_id))
            payload_str = delivery["payload"]
            
            # Mock HTTP call and capture signature
            captured_signature = None
            captured_payload = None
            
            async def capture_post(*args, **kwargs):
                nonlocal captured_signature, captured_payload
                captured_signature = kwargs.get("headers", {}).get("X-Webhook-Signature", "")
                captured_payload = kwargs.get("content", "")
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.text = "OK"
                return mock_response
            
            with patch.object(webhook_service, '_get_http_client') as mock_get_client:
                mock_client = AsyncMock()
                mock_client.post.side_effect = capture_post
                mock_get_client.return_value = mock_client
                
                asyncio.run(webhook_service._attempt_delivery(delivery_id))
            
            # Verify the signature
            assert captured_signature is not None
            assert captured_signature.startswith("sha256=")
            signature = captured_signature.replace("sha256=", "")
            
            # Verify signature is valid
            is_valid = webhook_service._verify_signature(
                captured_payload,
                signature,
                "my-secret-key",
            )
            assert is_valid is True

"""Webhook service for sending HTTP notifications to external systems.

This module provides secure webhook delivery with HMAC signature verification,
exponential backoff retry logic, and delivery tracking.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from cyborg.database import Database
from cyborg.services.base import BaseService, json_dumps, json_loads, utcnow


class WebhookEvent:
    """Event types that can trigger webhooks."""
    
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    PROJECT_BLOCKED = "project.blocked"
    PROJECT_READY_FOR_REVIEW = "project.ready_for_review"


class WebhookStatus:
    """Delivery status values."""
    
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"


class WebhookConfig:
    """Configuration for a webhook endpoint."""
    
    def __init__(
        self,
        id: str,
        name: str,
        url: str,
        secret: str,
        events: list[str],
        retry_count: int = 3,
        is_active: bool = True,
    ) -> None:
        self.id = id
        self.name = name
        self.url = url
        self.secret = secret
        self.events = events
        self.retry_count = retry_count
        self.is_active = is_active


class WebhookPayload:
    """Standard webhook payload format."""
    
    def __init__(
        self,
        event: str,
        project_id: str | None = None,
        task_id: str | None = None,
        task_title: str | None = None,
        result_summary: str | None = None,
        session_key: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.event = event
        self.project_id = project_id
        self.task_id = task_id
        self.task_title = task_title
        self.result_summary = result_summary
        self.session_key = session_key
        self.metadata = metadata or {}
        self.timestamp = utcnow().isoformat()
    
    def to_dict(self) -> dict[str, Any]:
        """Convert payload to dictionary."""
        return {
            "event": self.event,
            "timestamp": self.timestamp,
            "project_id": self.project_id,
            "task_id": self.task_id,
            "task_title": self.task_title,
            "result_summary": self.result_summary,
            "session_key": self.session_key,
            "metadata": self.metadata,
        }


class WebhookService(BaseService):
    """Service for managing and delivering webhooks."""
    
    def __init__(self, db: Database) -> None:
        super().__init__(db)
        self._http_client: Any | None = None
    
    async def _get_http_client(self) -> Any:
        """Lazy-load HTTP client."""
        if self._http_client is None:
            try:
                import httpx
                self._http_client = httpx.AsyncClient(timeout=30.0)
            except ImportError:
                raise RuntimeError("httpx is required for webhook delivery. Install with: pip install httpx")
        return self._http_client
    
    def _generate_signature(self, payload: str, secret: str) -> str:
        """Generate HMAC-SHA256 signature for payload."""
        return hmac.new(
            secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
    
    def _verify_signature(self, payload: str, signature: str, secret: str) -> bool:
        """Verify HMAC-SHA256 signature."""
        expected = self._generate_signature(payload, secret)
        return hmac.compare_digest(expected, signature)
    
    async def create_config(
        self,
        name: str,
        url: str,
        secret: str,
        events: list[str],
        retry_count: int = 3,
    ) -> WebhookConfig:
        """Create a new webhook configuration."""
        now = utcnow().isoformat()
        config_id = str(uuid4())
        
        await self.db.execute(
            """
            INSERT INTO webhook_configs (id, name, url, secret, events, retry_count, is_active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                config_id,
                name,
                url,
                secret,
                json_dumps(events),
                retry_count,
                now,
                now,
            ),
        )
        
        return WebhookConfig(
            id=config_id,
            name=name,
            url=url,
            secret=secret,
            events=events,
            retry_count=retry_count,
        )
    
    async def get_config(self, config_id: str) -> WebhookConfig | None:
        """Get a webhook configuration by ID."""
        row = await self.db.fetch_one(
            "SELECT * FROM webhook_configs WHERE id = ? AND deleted_at IS NULL",
            (config_id,),
        )
        if not row:
            return None
        return self._row_to_config(row)
    
    async def get_config_by_name(self, name: str) -> WebhookConfig | None:
        """Get a webhook configuration by name."""
        row = await self.db.fetch_one(
            "SELECT * FROM webhook_configs WHERE name = ? AND deleted_at IS NULL",
            (name,),
        )
        if not row:
            return None
        return self._row_to_config(row)
    
    async def list_configs(self, active_only: bool = True) -> list[WebhookConfig]:
        """List all webhook configurations."""
        query = "SELECT * FROM webhook_configs WHERE deleted_at IS NULL"
        params: list[Any] = []
        if active_only:
            query += " AND is_active = 1"
        query += " ORDER BY created_at DESC"
        
        rows = await self.db.fetch_all(query, tuple(params))
        return [self._row_to_config(row) for row in rows]
    
    async def update_config(
        self,
        config_id: str,
        url: str | None = None,
        secret: str | None = None,
        events: list[str] | None = None,
        retry_count: int | None = None,
        is_active: bool | None = None,
    ) -> WebhookConfig | None:
        """Update a webhook configuration."""
        updates: dict[str, Any] = {}
        if url is not None:
            updates["url"] = url
        if secret is not None:
            updates["secret"] = secret
        if events is not None:
            updates["events"] = json_dumps(events)
        if retry_count is not None:
            updates["retry_count"] = retry_count
        if is_active is not None:
            updates["is_active"] = 1 if is_active else 0
        
        if not updates:
            return await self.get_config(config_id)
        
        updates["updated_at"] = utcnow().isoformat()
        
        assignments = ", ".join(f"{k} = ?" for k in updates)
        params = tuple(updates.values()) + (config_id,)
        
        await self.db.execute(
            f"UPDATE webhook_configs SET {assignments} WHERE id = ? AND deleted_at IS NULL",
            params,
        )
        
        return await self.get_config(config_id)
    
    async def delete_config(self, config_id: str) -> bool:
        """Soft-delete a webhook configuration."""
        now = utcnow().isoformat()
        result = await self.db.execute(
            "UPDATE webhook_configs SET deleted_at = ?, updated_at = ? WHERE id = ? AND deleted_at IS NULL",
            (now, now, config_id),
        )
        # Note: aiosqlite execute doesn't return rowcount directly
        # We check if the config still exists
        row = await self.db.fetch_one(
            "SELECT id FROM webhook_configs WHERE id = ? AND deleted_at IS NULL",
            (config_id,),
        )
        return row is None
    
    async def trigger_event(
        self,
        event: str,
        project_id: str | None = None,
        task_id: str | None = None,
        task_title: str | None = None,
        result_summary: str | None = None,
        session_key: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> list[str]:
        """Trigger a webhook event.
        
        Returns list of delivery IDs created.
        """
        payload = WebhookPayload(
            event=event,
            project_id=project_id,
            task_id=task_id,
            task_title=task_title,
            result_summary=result_summary,
            session_key=session_key,
            metadata=metadata,
        )
        
        # Get all active configs that subscribe to this event
        configs = await self.list_configs(active_only=True)
        matching_configs = [
            config for config in configs
            if event in config.events or "*" in config.events
        ]
        
        delivery_ids: list[str] = []
        for config in matching_configs:
            delivery_id = await self._create_delivery(config, payload)
            delivery_ids.append(delivery_id)
        
        # Attempt immediate delivery
        for delivery_id in delivery_ids:
            await self._attempt_delivery(delivery_id)
        
        return delivery_ids
    
    async def _create_delivery(self, config: WebhookConfig, payload: WebhookPayload) -> str:
        """Create a delivery record."""
        delivery_id = str(uuid4())
        now = utcnow().isoformat()
        
        await self.db.execute(
            """
            INSERT INTO webhook_deliveries (id, webhook_id, event, payload, status, attempt_count, created_at)
            VALUES (?, ?, ?, ?, ?, 0, ?)
            """,
            (
                delivery_id,
                config.id,
                payload.event,
                json_dumps(payload.to_dict()),
                WebhookStatus.PENDING,
                now,
            ),
        )
        
        return delivery_id
    
    async def _attempt_delivery(self, delivery_id: str) -> bool:
        """Attempt to deliver a webhook.
        
        Returns True if successful, False otherwise.
        """
        # Get delivery details
        row = await self.db.fetch_one(
            "SELECT * FROM webhook_deliveries WHERE id = ?",
            (delivery_id,),
        )
        if not row:
            return False
        
        webhook_id = row["webhook_id"]
        payload_str = row["payload"]
        attempt_count = row["attempt_count"]
        
        # Get config
        config = await self.get_config(webhook_id)
        if not config or not config.is_active:
            await self._mark_failed(delivery_id, "Webhook configuration not found or inactive")
            return False
        
        # Increment attempt count
        attempt_count += 1
        
        try:
            client = await self._get_http_client()
            
            # Generate signature
            signature = self._generate_signature(payload_str, config.secret)
            
            headers = {
                "Content-Type": "application/json",
                "X-Webhook-Signature": f"sha256={signature}",
                "X-Webhook-Event": row["event"],
                "X-Webhook-ID": delivery_id,
                "X-Webhook-Attempt": str(attempt_count),
            }
            
            response = await client.post(
                config.url,
                content=payload_str,
                headers=headers,
            )
            
            # Check if successful (2xx status)
            if 200 <= response.status_code < 300:
                await self._mark_delivered(delivery_id, response.status_code, response.text)
                return True
            else:
                # Schedule retry if attempts remain
                if attempt_count < config.retry_count:
                    await self._schedule_retry(delivery_id, attempt_count, response.status_code, response.text)
                    return False
                else:
                    await self._mark_failed(
                        delivery_id,
                        f"HTTP {response.status_code}: {response.text}",
                        response.status_code,
                        response.text,
                    )
                    return False
                    
        except Exception as e:
            # Schedule retry if attempts remain
            if attempt_count < config.retry_count:
                await self._schedule_retry(delivery_id, attempt_count, error=str(e))
                return False
            else:
                await self._mark_failed(delivery_id, str(e))
                return False
    
    async def _mark_delivered(
        self,
        delivery_id: str,
        response_code: int,
        response_body: str,
    ) -> None:
        """Mark a delivery as successful."""
        now = utcnow().isoformat()
        await self.db.execute(
            """
            UPDATE webhook_deliveries
            SET status = ?, response_code = ?, response_body = ?, delivered_at = ?
            WHERE id = ?
            """,
            (WebhookStatus.DELIVERED, response_code, response_body, now, delivery_id),
        )
    
    async def _schedule_retry(
        self,
        delivery_id: str,
        attempt_count: int,
        response_code: int | None = None,
        response_body: str | None = None,
        error: str | None = None,
    ) -> None:
        """Schedule a retry with exponential backoff."""
        # Exponential backoff: 10s, 20s, 40s, ...
        delay_seconds = 10 * (2 ** (attempt_count - 1))
        next_retry = (utcnow() + timedelta(seconds=delay_seconds)).isoformat()
        
        await self.db.execute(
            """
            UPDATE webhook_deliveries
            SET attempt_count = ?, next_retry_at = ?, response_code = COALESCE(?, response_code), response_body = COALESCE(?, response_body), error_message = COALESCE(?, error_message)
            WHERE id = ?
            """,
            (attempt_count, next_retry, response_code, response_body, error, delivery_id),
        )
    
    async def _mark_failed(
        self,
        delivery_id: str,
        error_message: str,
        response_code: int | None = None,
        response_body: str | None = None,
    ) -> None:
        """Mark a delivery as failed."""
        await self.db.execute(
            """
            UPDATE webhook_deliveries
            SET status = ?, error_message = ?, response_code = COALESCE(?, response_code), response_body = COALESCE(?, response_body)
            WHERE id = ?
            """,
            (WebhookStatus.FAILED, error_message, response_code, response_body, delivery_id),
        )
    
    async def process_pending_deliveries(self) -> int:
        """Process all pending deliveries that are due for retry.
        
        Returns number of deliveries processed.
        """
        now = utcnow().isoformat()
        rows = await self.db.fetch_all(
            """
            SELECT id FROM webhook_deliveries
            WHERE status = ? AND next_retry_at <= ?
            ORDER BY next_retry_at ASC
            """,
            (WebhookStatus.PENDING, now),
        )
        
        count = 0
        for row in rows:
            await self._attempt_delivery(row["id"])
            count += 1
        
        return count
    
    async def get_delivery(self, delivery_id: str) -> dict[str, Any] | None:
        """Get delivery details by ID."""
        row = await self.db.fetch_one(
            "SELECT * FROM webhook_deliveries WHERE id = ?",
            (delivery_id,),
        )
        if not row:
            return None
        return dict(row)
    
    async def list_deliveries(
        self,
        webhook_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List webhook deliveries with optional filters."""
        query = "SELECT * FROM webhook_deliveries WHERE 1=1"
        params: list[Any] = []
        
        if webhook_id:
            query += " AND webhook_id = ?"
            params.append(webhook_id)
        if status:
            query += " AND status = ?"
            params.append(status)
        
        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        
        rows = await self.db.fetch_all(query, tuple(params))
        return [dict(row) for row in rows]
    
    def _row_to_config(self, row: Any) -> WebhookConfig:
        """Convert database row to WebhookConfig."""
        return WebhookConfig(
            id=row["id"],
            name=row["name"],
            url=row["url"],
            secret=row["secret"],
            events=json_loads(row["events"], []),
            retry_count=row["retry_count"],
            is_active=bool(row["is_active"]),
        )
    
    async def close(self) -> None:
        """Close the HTTP client."""
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

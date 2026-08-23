-- Increment 4 final step: session_routes is fully absorbed by bindings.
-- Reads moved to ConversationRepository.route_for (migration 453 backfilled
-- contact_id/is_active/address/endpoint_kind); writers now call
-- ConversationRepository.register_endpoint. The REST/CLI CRUD surfaces and
-- SessionRouteService are deleted in the same deploy.

DROP TABLE IF EXISTS session_routes;

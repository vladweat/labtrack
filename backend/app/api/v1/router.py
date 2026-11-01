"""Versioned API router composition."""

from fastapi import APIRouter

from app.modules.identity.router import router as identity_router
from app.modules.organizations.router import router as organizations_router
from app.modules.samples.router import router as samples_router
from app.modules.shipments.container_issues import router as container_issues_router
from app.modules.shipments.custody_incidents import router as custody_incidents_router
from app.modules.shipments.offline import router as courier_offline_router
from app.modules.shipments.receiving import router as receiving_router
from app.modules.shipments.router import router as shipments_router

api_router = APIRouter()
api_router.include_router(identity_router)
api_router.include_router(organizations_router)
api_router.include_router(samples_router)
api_router.include_router(shipments_router)
api_router.include_router(courier_offline_router)
api_router.include_router(container_issues_router)
api_router.include_router(custody_incidents_router)
api_router.include_router(receiving_router)

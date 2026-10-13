from uuid import uuid4

from app.modules.identity.access import MembershipGrant, Principal


def test_role_grant_is_limited_to_its_organization_and_region() -> None:
    organization_id = uuid4()
    region_id = uuid4()
    principal = Principal(
        user_id=uuid4(),
        email="manager@example.test",
        display_name="Lab Manager",
        memberships=(MembershipGrant("LAB_MANAGER", organization_id, region_id),),
    )

    assert principal.has_role("LAB_MANAGER", organization_id=organization_id)
    assert principal.has_role("LAB_MANAGER", region_id=region_id)
    assert not principal.has_role("LAB_MANAGER", organization_id=uuid4())
    assert not principal.has_role("LAB_MANAGER", region_id=uuid4())
    assert not principal.has_role("REGIONAL_OPERATIONS", region_id=region_id)

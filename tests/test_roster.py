"""The admin roster page: one sortable/filterable table, like the sibling apps."""
import pytest

from app.models import MemberKind


@pytest.mark.asyncio
async def test_roster_is_one_filterable_table(client, admin_cookie, make_member):
    student = await make_member("Riley Student", kind=MemberKind.student,
                                subteam_slug="software", subteam_label="Software")
    mentor = await make_member("Coach Casey", kind=MemberKind.mentor)

    resp = await client.get("/admin/roster", cookies={"mw_sso": admin_cookie})
    assert resp.status_code == 200
    assert "data-filter-sort" in resp.text
    assert "/static/js/table-filter-sort.js" in resp.text
    assert 'data-bs-target="#students-tab"' not in resp.text

    rows = {n: resp.text.split(n, 1)[1].split("</tr>", 1)[0] for n in (student.name, mentor.name)}
    assert 'data-label="Student"' in rows[student.name]
    assert 'data-label="Software"' in rows[student.name]
    assert 'data-label="Mentor"' in rows[mentor.name]
    # Students link to their review page; mentors have none.
    assert f'href="/admin/students/{student.member_code}"' in resp.text
    assert f'href="/admin/students/{mentor.member_code}"' not in resp.text

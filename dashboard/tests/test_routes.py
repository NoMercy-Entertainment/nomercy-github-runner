"""Where `/` goes, and what the navigation offers once it gets there.

The dashboard runs on the control plane, which has no container engine on
purpose (T-0603): every runner is reached through its worker's agent. There
is one fleet page and it lives at `/v2`, so `/` - the address people have and
type - is a redirect to it rather than a second copy of the same page.

One page, one address. Two links to the same grid, one of them a detour to
the page you are already on, is what made the reader think they were being
redirected somewhere.
"""


class TestTheHomePage:
    def test_it_redirects_to_the_fleet_page(self, client):
        r = client.get("/")
        assert r.status_code in (301, 302, 308)
        assert r.headers["Location"].endswith("/v2")

    def test_following_it_lands_on_the_fleet_page(self, client):
        r = client.get("/", follow_redirects=True)
        assert r.status_code == 200
        assert "fleet" in r.get_data(as_text=True).lower()

    def test_it_still_needs_a_session(self, anon_client):
        """The redirect must not become a way past the guard."""
        r = anon_client.get("/")
        assert r.status_code in (301, 302, 308)
        assert "/login" in r.headers["Location"]


class TestWhatTheNavigationOffers:
    def test_the_fleet_page_links_itself_at_the_address_it_has(self, client):
        page = client.get("/v2").get_data(as_text=True)
        assert 'href="/v2" class="on">Fleet<' in page

    def test_no_page_still_offers_the_removed_v1_grid(self, client):
        for path in ("/v2", "/history", "/settings"):
            page = client.get(path).get_data(as_text=True)
            assert 'href="/"' not in page, path
            assert "Fleet v2" not in page, path

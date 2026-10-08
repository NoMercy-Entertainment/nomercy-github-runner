"""Where `/` goes, and what the navigation offers once it gets there.

The dashboard runs on the control plane, which has no container engine on
purpose (T-0603): every runner is reached through its worker's agent. There
is one fleet page and it lives at `/` - the address people have and type -
so `/v2`, the old address, is a redirect to it rather than a second copy of
the same page kept alive for old links.

One page, one address. Two links to the same grid, one of them a detour to
the page you are already on, is what made the reader think they were being
redirected somewhere.
"""


class TestTheHomePage:
    def test_it_serves_the_fleet_page(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "fleet" in r.get_data(as_text=True).lower()

    def test_it_still_needs_a_session(self, anon_client):
        r = anon_client.get("/")
        assert r.status_code in (301, 302, 308)
        assert "/login" in r.headers["Location"]


class TestTheOldAddress:
    def test_it_redirects_to_the_fleet_page(self, client):
        r = client.get("/v2")
        assert r.status_code in (301, 302, 308)
        assert r.headers["Location"].endswith("/")

    def test_following_it_lands_on_the_fleet_page(self, client):
        r = client.get("/v2", follow_redirects=True)
        assert r.status_code == 200
        assert "fleet" in r.get_data(as_text=True).lower()

    def test_it_still_needs_a_session(self, anon_client):
        """The redirect must not become a way past the guard."""
        r = anon_client.get("/v2")
        assert r.status_code in (301, 302, 308)
        assert "/login" in r.headers["Location"]


class TestWhatTheNavigationOffers:
    def test_the_fleet_page_links_itself_at_the_address_it_has(self, client):
        page = client.get("/").get_data(as_text=True)
        assert 'href="/" class="on" aria-current="page">Fleet<' in page

    def test_no_page_still_offers_the_old_v2_address(self, client):
        """The nav points at `/` everywhere now; `/v2` is a redirect, not a
        second link for pages to keep offering."""
        import history

        history.init()          # /history reads it to fill its filters
        for path in ("/", "/history", "/settings"):
            page = client.get(path).get_data(as_text=True)
            assert 'href="/v2"' not in page, path
            assert "Fleet v2" not in page, path

package runner

import "testing"

func TestNoMercyRelativeActionSymlinksStayWithinParent(t *testing.T) {
    for _, link := range []string{"../outside", "../../outside", "/outside"} {
        if _, err := symlinkJoin("action.yml", link, "."); err == nil {
            t.Fatalf("outside local action link accepted: %s", link)
        }
    }
    if value, err := symlinkJoin("dir/action.yml", "../other.yml", "."); err != nil || value != "other.yml" {
        t.Fatal("valid internal relative link refused")
    }
}

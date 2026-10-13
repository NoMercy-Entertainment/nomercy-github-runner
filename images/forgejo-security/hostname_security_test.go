package git

import "testing"

func TestNoMercyRejectsLookalikeProviderHosts(t *testing.T) {
    for _, value := range []string{
        "https://githubXcom/owner/repo", "https://github.com.attacker.test/owner/repo",
        "https://attacker.test/github.com/owner/repo", "git@githubXcom:owner/repo",
        "git@github.com.attacker.test:owner/repo", "https://git-codecommit.us-east-1.amazonawsXcom/v1/repos/repo",
        "ssh://git-codecommit.us-east-1.amazonawsXcom/v1/repos/repo",
    } {
        provider, slug, err := findSlug(value, "github.com")
        if err != nil || provider != "" || slug != value { t.Fatalf("lookalike host accepted: %s", value) }
    }
}

func TestNoMercyRejectsLookalikeConfiguredHosts(t *testing.T) {
    for _, value := range []string{"https://exampleXcom/owner/repo", "https://example.com.attacker.test/owner/repo", "ssh://git@fakeexample.com/owner/repo"} {
        provider, slug, err := findSlug(value, "example.com")
        if err != nil || provider != "" || slug != value { t.Fatalf("configured host lookalike accepted: %s", value) }
    }
}

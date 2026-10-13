package filecollector

import (
    "archive/tar"
    "os"
    "path/filepath"
    "strings"
    "testing"
)

func TestNoMercyCopyRejectsArchiveEscapes(t *testing.T) {
    for _, entry := range []struct{name, link string}{
        {"../outside", ""}, {"dir/../../outside", ""}, {"/outside", ""},
        {"link", "../outside"}, {"link", "/outside"}, {"dir/link", "../../outside"},
    } {
        t.Run(entry.name+entry.link, func(t *testing.T) {
            base := t.TempDir()
            destination := filepath.Join(base, "destination")
            collector := CopyCollector{DstDir: destination}
            header := tar.Header{Name: entry.name, Mode: 0600}
            if err := collector.WriteFile(entry.name, header.FileInfo(), entry.link, strings.NewReader("untrusted")); err == nil {
                t.Fatal("archive escape accepted")
            }
            if _, err := os.Stat(filepath.Join(base, "outside")); !os.IsNotExist(err) {
                t.Fatal("archive wrote outside destination")
            }
        })
    }
}

func TestNoMercyCopyRejectsExistingOutsideSymlink(t *testing.T) {
    base := t.TempDir()
    destination := filepath.Join(base, "destination")
    outside := filepath.Join(base, "outside")
    if err := os.MkdirAll(destination, 0700); err != nil { t.Fatal(err) }
    if err := os.MkdirAll(outside, 0700); err != nil { t.Fatal(err) }
    if err := os.Symlink(outside, filepath.Join(destination, "redirect")); err != nil { t.Skip("symlink unavailable") }
    collector := CopyCollector{DstDir: destination}
    header := tar.Header{Name: "redirect/file", Mode: 0600}
    if err := collector.WriteFile(header.Name, header.FileInfo(), "", strings.NewReader("untrusted")); err == nil {
        t.Fatal("existing outside symlink followed")
    }
    if _, err := os.Stat(filepath.Join(outside, "file")); !os.IsNotExist(err) { t.Fatal("outside file written") }
}

func TestNoMercyCopyKeepsRegularFilesAndInternalLinks(t *testing.T) {
    destination := filepath.Join(t.TempDir(), "destination")
    collector := CopyCollector{DstDir: destination}
    header := tar.Header{Name: "dir/file", Mode: 0600}
    if err := collector.WriteFile(header.Name, header.FileInfo(), "", strings.NewReader("expected")); err != nil { t.Fatal(err) }
    header.Name = "dir/link"
    if err := collector.WriteFile(header.Name, header.FileInfo(), "file", nil); err != nil { t.Fatal(err) }
    body, err := os.ReadFile(filepath.Join(destination, "dir/link"))
    if err != nil || string(body) != "expected" { t.Fatal("internal link or content lost", err) }
}

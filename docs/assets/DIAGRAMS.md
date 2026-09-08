# README diagrams

The README uses rendered PNGs so the diagrams do not depend on GitHub's live
Mermaid renderer. Each diagram has light and dark variants selected through
the README's `picture` elements.

- [Architecture source](architecture.mmd)
- [History synchronization source](history-sync.mmd)

Render with Mermaid CLI 11.17.0 from the repository root:

```bash
for diagram in architecture history-sync; do
  npx --yes --package @mermaid-js/mermaid-cli@11.17.0 mmdc \
    -i "docs/assets/$diagram.mmd" -o "docs/assets/$diagram-light.png" \
    -t default -b transparent -w 1500 -s 2
  npx --yes --package @mermaid-js/mermaid-cli@11.17.0 mmdc \
    -i "docs/assets/$diagram.mmd" -o "docs/assets/$diagram-dark.png" \
    -t dark -b transparent -w 1500 -s 2
done
```

Mermaid CLI requires a working Chromium installation. If it cannot find the
browser, supply a Puppeteer JSON configuration with `-p`, including an
`executablePath` for your local Chromium installation.

Check both themes visually after regenerating the images, including the labels
on arrows and the diagram's appearance at the README's display width.

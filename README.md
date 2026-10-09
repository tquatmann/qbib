# qbib
**Q**uickly search DBLP from command line and get **bib**tex output


## Setup

qbib searches a local index of the DBLP data dump (the DBLP search API is protected by a bot check).
Build or refresh it with

```
python3 qbib.py --update
```

This streams the latest `dblp.xml.gz` (about 1 GB) from DBLP and indexes it into `~/.cache/qbib/dblp.sqlite`
(set `QBIB_DATA_DIR` to change the location). It takes a few minutes; re-running it does nothing if the dump is unchanged.

## Usage

Search DBLP for some search terms, e.g. `donald knuth`:

```
python3 qbib.py donald knuth
````

This yields a list of entries

```
 1. 2022: Donald E. Knuth
    All Questions Answered (Invited Talk). CP
 2. 2021: Donald E. Knuth, Len Shustek
    Let&apos;s not dumb down the history of computer science. Commun. ACM
 3. 2015: William I. Gasarch
    Review of: Algorithmic Barriers Falling: P=NP? by Donald E. Knuth and Edgar G. Daylight and The Essential Knuth by Donald E. Knuth and Edgar G. Daylight. SIGACT News
...
Select an entry [1-20] (quit, condensed, open url):
```

Replying the prompt with an entry, e.g., `3` prints the corresponding bibtex entry and puts it into the clipboard

```
-------------------------------- DBLP  STANDARD --------------------------------
@article{DBLP:journals/sigact/Gasarch15e,
  author       = {William I. Gasarch},
  title        = {Review of: Algorithmic Barriers Falling: P=NP? by Donald E. Knuth
                  and Edgar G. Daylight and The Essential Knuth by Donald E. Knuth and
                  Edgar G. Daylight},
  journal      = {{SIGACT} News},
  volume       = {46},
  number       = {2},
  pages        = {21--22},
  year         = {2015},
  doi          = {10.1145/2789149.2789155}
}
--------------------------------------------------------------------------------
Copied to clipboard.
```

We can use dblp's condensed bibtex entry by typing `3c` instead. To additionally open the url in a browser, we may type `3o` or `3co`.
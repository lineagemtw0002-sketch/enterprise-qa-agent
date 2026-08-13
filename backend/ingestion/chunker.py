def chunk_text(text: str, chunk_size: int, chunk_overlap: int) -> list[str]:
    """Simple recursive-ish splitter: break on paragraphs first, then hard-wrap
    anything still too long. Overlap keeps context from being severed mid-idea."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]

    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= chunk_size:
            current = candidate
            continue

        if current:
            chunks.append(current)
        if len(para) <= chunk_size:
            current = para
        else:
            for i in range(0, len(para), chunk_size - chunk_overlap):
                chunks.append(para[i : i + chunk_size])
            current = ""

    if current:
        chunks.append(current)

    # apply overlap between adjacent chunks
    overlapped: list[str] = []
    for i, chunk in enumerate(chunks):
        if i == 0:
            overlapped.append(chunk)
            continue
        prefix = chunks[i - 1][-chunk_overlap:]
        overlapped.append(f"{prefix}{chunk}")

    return overlapped

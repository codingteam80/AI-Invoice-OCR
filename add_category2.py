path = "models/invoice.py"
content = open(path, encoding="utf-8").read()

old = "    payment_terms: Optional[str] = None\n\n    source_file: Optional[str] = None"
new = "    payment_terms: Optional[str] = None\n    category: str = \"Others\"\n\n    source_file: Optional[str] = None"

if old in content:
    content = content.replace(old, new)
    open(path, "w", encoding="utf-8").write(content)
    print("category field added to Invoice model.")
else:
    print("Pattern not found - no changes made.")

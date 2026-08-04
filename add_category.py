path = "database/models.py"
content = open(path, encoding="utf-8").read()

old = "    payment_terms = Column(String, nullable=True)\n    source_file = Column(String, nullable=True)"
new = "    payment_terms = Column(String, nullable=True)\n    category = Column(String, default=\"Others\", nullable=False)\n    source_file = Column(String, nullable=True)"

if old in content:
    content = content.replace(old, new)
    open(path, "w", encoding="utf-8").write(content)
    print("category column added back.")
else:
    print("Pattern not found - no changes made.")

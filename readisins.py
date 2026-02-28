def read_isins_from_file(path: str = "/Users/ishabaev/python_projects/shabshab/isins.txt") -> list[str]:
    isins: list[str] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            isin = line.strip()
            if isin:
                isins.append(isin)
    return isins

print(read_isins_from_file())  # Пример вызова
    
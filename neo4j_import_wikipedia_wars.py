import logging
import os
from dataclasses import dataclass
from typing import List, Optional, Tuple

import requests
from bs4 import BeautifulSoup, Tag
from neo4j import GraphDatabase


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)


class SectionNotFoundException(Exception):
    pass


class TableNotFoundException(Exception):
    pass


@dataclass
class TableSectionResult:
    header_html: str
    row_html: List[str]


class WikipediaWarImporter:
    def __init__(self, neo4j_uri: str, neo4j_user: str, neo4j_password: str):
        self.driver = GraphDatabase.driver(neo4j_uri, auth=(neo4j_user, neo4j_password))
        self.http = requests.Session()
        self.http.headers.update({
            "User-Agent": "WikipediaWarImporter/1.0 (requests)"
        })

    def close(self) -> None:
        self.driver.close()
        self.http.close()

    def run(self) -> None:
        sections = self._load_sections()
        logging.info("Found %d sections to process", len(sections))

        for record in sections:
            section_element_id = record["sectionElementId"]
            section_name = record["sectionName"]
            page_url = record["pageUrl"]

            logging.info("Processing section='%s' page='%s'", section_name, page_url)

            try:
                result = self.extract_table_section(page_url, section_name)
                self._write_result(section_element_id, result)
                logging.info(
                    "Updated section='%s' with header and %d war rows",
                    section_name,
                    len(result.row_html)
                )
            except SectionNotFoundException as e:
                logging.warning("Skipping section='%s': %s", section_name, e)
            except TableNotFoundException as e:
                logging.warning("Skipping section='%s': %s", section_name, e)
            except Exception:
                logging.exception("Unexpected error while processing section='%s'", section_name)

    def _load_sections(self) -> List[dict]:
        query = """
        MATCH (s:Section)-[:PART_OF]->(p:Page)
        RETURN elementId(s) AS sectionElementId,
               s.name AS sectionName,
               p.url AS pageUrl
        ORDER BY p.url, s.name
        """
        with self.driver.session() as session:
            result = session.run(query)
            return [record.data() for record in result]

    def _write_result(self, section_element_id: str, result: TableSectionResult) -> None:
        query = """
        MATCH (s:Section)
        WHERE elementId(s) = $sectionElementId

        SET s.header = $headerHtml

        OPTIONAL MATCH (w:War)-[:IN]->(s)
        DETACH DELETE w

        WITH s
        UNWIND $rows AS rowHtml
        CREATE (w:War {html: rowHtml})
        CREATE (w)-[:IN]->(s)
        """
        with self.driver.session() as session:
            session.run(
                query,
                sectionElementId=section_element_id,
                headerHtml=result.header_html,
                rows=result.row_html
            ).consume()

    def extract_table_section(self, url: str, section_name: Optional[str]) -> TableSectionResult:
        html = self._fetch_html(url)
        soup = BeautifulSoup(html, "html.parser")

        content_root = self._find_article_content_root(soup)
        if content_root is None:
            raise RuntimeError("Could not locate Wikipedia article content")

        if section_name is None or not section_name.strip():
            table = self._find_first_table_in_root(content_root)
            if table is None:
                raise TableNotFoundException("No table found on page")
        else:
            heading = self._find_matching_heading(content_root, section_name)
            if heading is None:
                raise SectionNotFoundException(f"No section found containing '{section_name}'")

            table = self._find_first_table_in_section(heading)
            if table is None:
                raise TableNotFoundException(
                    f"No table found under section '{self._heading_text(heading)}'"
                )

        header_html, row_html = self._extract_header_and_rows(table)
        return TableSectionResult(header_html=header_html, row_html=row_html)

    def _fetch_html(self, url: str) -> str:
        response = self.http.get(url, timeout=30)
        response.raise_for_status()
        return response.text

    def _find_article_content_root(self, soup: BeautifulSoup) -> Optional[Tag]:
        # Most reliable on Wikipedia article HTML
        root = soup.select_one("div.mw-parser-output")
        if root is not None:
            return root

        # Fallback
        root = soup.select_one("#mw-content-text")
        if root is not None:
            return root

        return None

    def _find_matching_heading(self, content_root: Tag, section_name: str) -> Optional[Tag]:
        wanted = self._normalize(section_name)

        candidates = content_root.find_all(["h2", "h3", "h4", "h5", "h6"])
        for heading in candidates:
            text = self._normalize(self._heading_text(heading))
            if text and wanted in text:
                return heading

        return None

    def _heading_text(self, heading: Tag) -> str:
        headline = heading.select_one(".mw-headline")
        if headline is not None:
            return headline.get_text(" ", strip=True)
        return heading.get_text(" ", strip=True)

    def _find_first_table_in_section(self, heading: Tag) -> Optional[Tag]:
        start_level = self._heading_level(heading.name)

        for node in heading.find_all_next():
            if not isinstance(node, Tag):
                continue

            if node.name in {"h2", "h3", "h4", "h5", "h6"}:
                if self._heading_level(node.name) <= start_level:
                    break

            if node.name == "table" and self._is_data_table(node):
                return node

        return None

    def _find_first_table_in_root(self, content_root: Tag) -> Optional[Tag]:
        for table in content_root.find_all("table"):
            if self._is_data_table(table):
                return table
        return None

    def _is_data_table(self, table: Tag) -> bool:
        classes = set(table.get("class", []))

        bad_classes = {
            "box-Expand_section",
            "ambox",
            "cmbox",
            "metadata",
            "plainlinks",
            "vertical-navbox",
            "navbox"
        }
        if classes.intersection(bad_classes):
            return False

        rows = self._get_table_rows(table)
        if len(rows) < 2:
            return False

        th_count = 0
        td_count = 0
        for row in rows:
            for child in row.children:
                if not isinstance(child, Tag):
                    continue
                if child.name == "th":
                    th_count += 1
                elif child.name == "td":
                    td_count += 1

        return th_count > 0 and td_count >= 4

    def _extract_header_and_rows(self, table: Tag) -> Tuple[str, List[str]]:
        thead = table.find("thead", recursive=False)
        if thead is not None:
            header_html = str(thead)
            all_rows = self._get_table_rows(table)
            header_row_count = self._count_rows_in_container(thead)
            row_html = [str(row) for row in all_rows[header_row_count:]]
            return header_html, row_html

        rows = self._get_table_rows(table)
        if not rows:
            return "", []

        header_rows: List[Tag] = []
        data_start_index = 0

        for i, row in enumerate(rows):
            has_direct_th = self._row_has_direct_cell(row, "th")
            has_direct_td = self._row_has_direct_cell(row, "td")

            if has_direct_th and not has_direct_td:
                header_rows.append(row)
                data_start_index = i + 1
            elif has_direct_th and not header_rows:
                # First mixed row can still be header on Wikipedia
                header_rows.append(row)
                data_start_index = i + 1
            elif header_rows:
                data_start_index = i
                break
            else:
                data_start_index = i
                break

        header_html = "".join(str(row) for row in header_rows)
        row_html = [str(row) for row in rows[data_start_index:]]
        return header_html, row_html

    def _get_table_rows(self, table: Tag) -> List[Tag]:
        rows: List[Tag] = []

        for child in table.children:
            if not isinstance(child, Tag):
                continue

            if child.name == "tr":
                rows.append(child)
            elif child.name in {"thead", "tbody", "tfoot"}:
                rows.extend(child.find_all("tr", recursive=False))

        return rows

    def _count_rows_in_container(self, container: Tag) -> int:
        count = 0
        for child in container.children:
            if not isinstance(child, Tag):
                continue

            if child.name == "tr":
                count += 1
            elif child.name in {"thead", "tbody", "tfoot"}:
                count += len(child.find_all("tr", recursive=False))
        return count

    def _row_has_direct_cell(self, row: Tag, cell_name: str) -> bool:
        for child in row.children:
            if isinstance(child, Tag) and child.name == cell_name:
                return True
        return False

    @staticmethod
    def _heading_level(tag_name: str) -> int:
        return int(tag_name[1])

    @staticmethod
    def _normalize(value: str) -> str:
        return value.replace("\xa0", " ").strip().lower()


def main() -> None:
    neo4j_uri = os.getenv("NEO4J_URI", "neo4j://localhost:7687")
    neo4j_user = os.getenv("NEO4J_USER", "neo4j")
    neo4j_password = os.getenv("NEO4J_PASSWORD", "password")

    importer = WikipediaWarImporter(neo4j_uri, neo4j_user, neo4j_password)
    try:
        importer.run()
    finally:
        importer.close()


if __name__ == "__main__":
    main()

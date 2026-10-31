# CV Profile Statement Templates

## Base Profile
Principal Android Software Engineer with 10+ years of experience defining technical strategy and architecture for large-scale mobile platforms. Proven track record of leading cross-functional teams through major architectural transformations across Android and distributed backend systems. Trusted by leadership to foster engineering excellence, standardize best practices, reduce technical debt, and align technical initiatives with business value.

## Variant 1: Mobile Architect / DevEx Focus
Principal Mobile Architect and Engineering Leader with 10+ years of experience designing scalable mobile-to-backend interfaces and transforming developer workflows. Expert in Gradle build optimization, CI/CD automation, and instituting a 'best-practice-by-default' philosophy that reduces onboarding time and accelerates feature delivery. Proven ability to mentor engineering teams and align high-impact technical debt reduction with business goals.

## Variant 2: Full-Stack / Platform Engineering Focus
Senior Engineering Leader with over a decade of experience spanning Android architecture, full-stack application development, and modern API standards. Demonstrated success in orchestrating major platform migrations, including zero-downtime authentication systems and cross-platform design integrations. Adept at bridging the gap between product strategy and technical execution using modern frameworks (Kotlin, React Native, Firebase, GCP).

# Master Template Styling Guidelines (MANDATORY)

All generated CVs MUST adhere to the following stylistic rules established in the master template (`cv/main_example.tex`):

### 1. Colors & Fonts
- **Theme Color:** Use `#2F5597` (defined as `color1`) for all primary headings, titles, and dates.
- **Heading Font:** Use **Inconsolata** (fixed-width) for all `H*` elements: Name (`H1`), Section Headings (`H2`), Competency Categories (`H3`), and Job Titles (`H3/H4`).
- **Body Font:** Use a clean **Sans-serif** font (e.g., `tgheros` / Helvetica clone) for all body text, bullets, and contact info to ensure maximum legibility.
- **Encoding:** Always use `\usepackage[T1]{fontenc}` to ensure characters like `|` render correctly as vertical bars.

### 2. Layout & Hierarchy
- **Header:** Single-line centered header on the first page only, using `|` as a separator (e.g., `Phone | Email | LinkedIn | ...`).
- **Areas of Expertise:** Distributed 3-column layout (Left | Center | Right) using `\tabular*` spanning the full text width. Use `\renewcommand{\arraystretch}{1.5}` for generous vertical spacing. **NO vertical bars** in this section.
- **Experience Items:**
  - **Line 1:** Job Title (left) and Dates (right) in `H3/H4` (Inconsolata, bold, `color1`).
  - **Line 2:** Company (left) and Location (right) in a lower hierarchy (`H4/H5`).
- **Links:** All links must be explicitly underlined (`\uline`) for a11y compliance, with NO boxes around them.

### 3. Constraints
- **Max Pages:** 2 pages.
- **No Page Numbers:** `\pagestyle{empty}`.
- **Margins:** 0.5-inch margins (`[margin=0.5in]{geometry}`).

"""Made-up candidates so a new user can try search before adding real people."""
from __future__ import annotations

from .store import Store

SAMPLES = [
    ("Maya Okafor", "Senior Backend Engineer", "Ledgerline", "Austin, TX",
     "Eight years building payment systems in Go and Rust. Led the migration of a card-processing service to event sourcing; mentors two junior engineers.", "go, rust, payments"),
    ("Daniel Reyes", "Staff Frontend Engineer", "Brightpath", "San Francisco, CA",
     "React and TypeScript specialist who built a design system used by 40 product teams. Strong on accessibility and performance budgets.", "react, typescript, design systems"),
    ("Priya Natarajan", "Data Science Lead", "Harvest Analytics", "New York, NY",
     "Runs a team of six doing forecasting and causal inference in Python. Previously an economist; publishes on experiment design.", "python, causal inference, leadership"),
    ("Tomás Lindqvist", "Machine Learning Engineer", "Northwind AI", "Seattle, WA",
     "Trains and serves ranking models with PyTorch on Kubernetes. Cut inference cost 40% with distillation and batching.", "pytorch, mlops, kubernetes"),
    ("Aisha Bello", "Product Manager", "Cartwheel", "Chicago, IL",
     "B2B SaaS PM focused on onboarding and activation. Shipped a self-serve trial that doubled conversion; former support lead.", "saas, growth, onboarding"),
    ("Kenji Watanabe", "Site Reliability Engineer", "Cloudmere", "Remote (Portland, OR)",
     "On-call lead for a 99.99% uptime API platform. Terraform, Prometheus and incident command; writes great postmortems.", "sre, terraform, observability"),
    ("Sofia Marchetti", "Engineering Manager", "Ledgerline", "Austin, TX",
     "Manages two backend teams (14 engineers). Grew the org from 5 and introduced a career ladder. Hands-on background in Java.", "management, java, hiring"),
    ("Liam O'Connor", "iOS Engineer", "Fitloop", "Boston, MA",
     "Swift and SwiftUI developer on a top-50 fitness app. Owns HealthKit integrations and offline sync.", "swift, ios, mobile"),
    ("Grace Kim", "Security Engineer", "Vaultline", "Washington, DC",
     "Application security for fintech: threat modeling, secure code review and SOC 2 programs. Former penetration tester.", "appsec, soc2, fintech"),
    ("Arjun Mehta", "Full Stack Engineer", "Shoplane", "San Jose, CA",
     "Next.js, Node and Postgres generalist at an e-commerce startup. Built the checkout flow and an internal admin panel.", "nextjs, node, postgres"),
    ("Hannah Schultz", "Technical Recruiter", "Talentforge", "Denver, CO",
     "Agency recruiter placing infrastructure and data engineers. Strong network in the Rockies startup scene.", "recruiting, infrastructure"),
    ("Omar Haddad", "Data Engineer", "Streamwise", "New York, NY",
     "Builds batch and streaming pipelines with Spark, Kafka and dbt. Owns the company's data warehouse on Snowflake.", "spark, kafka, dbt, snowflake"),
    ("Chloe Dubois", "Product Designer", "Brightpath", "Los Angeles, CA",
     "Systems-minded designer working across web and mobile. Ran the research program behind a full navigation redesign.", "figma, research, design systems"),
    ("Rafael Costa", "Solutions Engineer", "Northwind AI", "Miami, FL",
     "Pre-sales engineer for an AI platform. Runs technical demos and pilots with enterprise customers; Python and SQL.", "pre-sales, python, enterprise"),
    ("Emily Zhang", "Senior Software Engineer", "Medisight", "Boston, MA",
     "Healthcare software in Python and Django; HL7/FHIR integrations with hospital systems. Cares about data privacy.", "python, django, healthcare"),
    ("Noah Williams", "DevOps Engineer", "Shoplane", "Remote (Atlanta, GA)",
     "CI/CD and AWS cost work. Moved builds from Jenkins to GitHub Actions and halved deploy times.", "aws, ci/cd, github actions"),
]


def load_sample_data(store: Store) -> int:
    added = 0
    for name, title, company, location, about, tags in SAMPLES:
        slug = name.lower().replace(" ", ".").replace("'", "")
        _, created = store.upsert_candidate(
            name=name, current_title=title, current_company=company, location=location,
            about=about, tags=tags, email=f"{slug}@example.com", source="Sample data",
        )
        added += created
    return added

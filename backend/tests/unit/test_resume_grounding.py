from app.services.resume_grounding import source_text, unsupported_claims

SOURCE = source_text(
    [
        "Jane Doe. Senior Engineer at Acme Corp, 2019 - 2023. Built PostgreSQL and AWS data "
        "pipelines; cut costs by 40%. Led a team of 5. Skills: Python, Node.js, C++."
    ],
    {"education": [{"school": "State University", "year": "2018"}]},
)


def test_reordered_and_reworded_resume_is_supported():
    markdown = (
        "# Jane Doe\n## Skills\nPython, C++, Node.js\n## Experience\n"
        "### Senior Engineer, Acme Corp (2019 - 2023)\n"
        "- Led a team of 5 and cut costs by 40% with AWS and PostgreSQL pipelines\n"
        "## Education\nState University, 2018\n"
    )
    assert unsupported_claims(markdown, SOURCE) == []


def test_invented_numbers_and_tools_are_reported():
    markdown = (
        "- Cut costs by 60% using Kubernetes and TensorFlow\n- Managed a team of 12 at Google\n"
    )
    assert unsupported_claims(markdown, SOURCE) == [
        "60%",
        "Kubernetes",
        "TensorFlow",
        "12",
        "Google",
    ]


def test_missing_job_keywords_are_not_added_silently():
    # JD asks for Terraform; the candidate never mentioned it.
    assert unsupported_claims("Skills: Python, Terraform", SOURCE) == ["Terraform"]


def test_headings_months_and_sentence_starts_are_not_flagged():
    markdown = "## Work Experience\n- Built reliable systems. Delivered results in March\n"
    assert unsupported_claims(markdown, SOURCE) == []

"""Target schema: a subset of HubSpot contact properties.

Week 3 replaces the hard-coded list with the live definitions from
GET /crm/v3/properties/contacts; the shape stays the same.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TargetProperty:
    name: str
    label: str
    description: str
    type: str  # string | email | phone | url | enum
    synonyms: tuple[str, ...] = ()
    options: tuple[str, ...] = ()  # enum values
    option_synonyms: dict[str, str] = field(default_factory=dict)  # normalized source value -> option


LIFECYCLE_SYNONYMS = {
    # subscriber
    "subscriber": "subscriber", "subscribed": "subscriber", "newsletter": "subscriber",
    "newsletter only": "subscriber", "mailing list": "subscriber",
    # lead
    "lead": "lead", "new lead": "lead", "new": "lead", "inquiry": "lead", "enquiry": "lead",
    "interested": "lead", "contacted": "lead",
    # marketing / sales qualified
    "mql": "marketingqualifiedlead", "marketing qualified": "marketingqualifiedlead",
    "marketing qualified lead": "marketingqualifiedlead",
    "sql": "salesqualifiedlead", "sales qualified": "salesqualifiedlead",
    "sales qualified lead": "salesqualifiedlead", "qualified": "salesqualifiedlead",
    # opportunity
    "opportunity": "opportunity", "opp": "opportunity", "prospect": "opportunity",
    "in negotiation": "opportunity", "negotiation": "opportunity", "proposal sent": "opportunity",
    # customer
    "customer": "customer", "client": "customer", "active": "customer", "active customer": "customer",
    "paying": "customer", "paid": "customer", "won": "customer", "closed won": "customer",
    "member": "customer",
    # evangelist
    "evangelist": "evangelist", "advocate": "evangelist", "promoter": "evangelist",
    # other
    "other": "other", "churned": "other", "inactive": "other", "former customer": "other",
    "lost": "other", "closed lost": "other", "cancelled": "other", "canceled": "other",
}

TARGET_PROPERTIES: tuple[TargetProperty, ...] = (
    TargetProperty("email", "Email", "Contact's email address", "email",
                   ("email", "e mail", "email address", "mail", "emailaddress", "primary email",
                    "work email", "contact email")),
    TargetProperty("firstname", "First name", "Contact's first (given) name", "string",
                   ("first name", "firstname", "fname", "given name", "forename", "first")),
    TargetProperty("lastname", "Last name", "Contact's last (family) name", "string",
                   ("last name", "lastname", "lname", "surname", "family name", "last")),
    TargetProperty("phone", "Phone number", "Contact's primary phone number", "phone",
                   ("phone", "phone number", "telephone", "tel", "tel no", "telephone number",
                    "work phone", "office phone", "contact number", "phone no", "landline")),
    TargetProperty("mobilephone", "Mobile phone number", "Contact's mobile/cell phone number", "phone",
                   ("mobile", "mobile phone", "cell", "cell phone", "mobile number", "cellphone")),
    TargetProperty("company", "Company name", "Name of the company or organization the contact works for",
                   "string", ("company", "company name", "organization", "organisation", "org",
                              "employer", "business", "account name", "firm")),
    TargetProperty("jobtitle", "Job title", "Contact's job title or role", "string",
                   ("job title", "title", "jobtitle", "position", "role", "designation", "job role")),
    TargetProperty("website", "Website URL", "Website of the contact or their company", "url",
                   ("website", "web", "url", "site", "homepage", "web site", "company website")),
    TargetProperty("address", "Street address", "Street address line", "string",
                   ("address", "street", "street address", "address line 1", "address1", "addr")),
    TargetProperty("city", "City", "City the contact is located in", "string",
                   ("city", "town", "location city", "city name")),
    TargetProperty("state", "State/Region", "State, province or region", "string",
                   ("state", "province", "region", "state province", "county")),
    TargetProperty("zip", "Postal code", "ZIP or postal code", "string",
                   ("zip", "zip code", "zipcode", "postal code", "postcode", "post code", "pin code")),
    TargetProperty("country", "Country/Region", "Country of the contact", "string",
                   ("country", "country name", "nation", "country region")),
    TargetProperty("lifecyclestage", "Lifecycle stage",
                   "Stage of the relationship: subscriber, lead, opportunity, customer, etc.", "enum",
                   ("lifecycle stage", "lifecyclestage", "stage", "status", "account status",
                    "customer status", "contact status", "lead stage", "customer type"),
                   options=("subscriber", "lead", "marketingqualifiedlead", "salesqualifiedlead",
                            "opportunity", "customer", "evangelist", "other"),
                   option_synonyms=LIFECYCLE_SYNONYMS),
)

BY_NAME: dict[str, TargetProperty] = {p.name: p for p in TARGET_PROPERTIES}

# Plain-language meaning of each enum option, offered to a decision model.
OPTION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "lifecyclestage": {
        "subscriber": "Signed up for updates or a newsletter; no buying interest shown yet",
        "lead": "Showed some interest or made contact; not yet qualified",
        "marketingqualifiedlead": "Engaged enough for marketing to consider them ready for sales",
        "salesqualifiedlead": "Sales has confirmed they are a real potential buyer",
        "opportunity": "Actively in a sales conversation, proposal or deal",
        "customer": "Currently paying, active, or has purchased",
        "evangelist": "Customer who actively promotes or refers others",
        "other": "Former, churned, lost, inactive, or fits no other stage",
    },
}

# Options offered to a decision model for column mapping (plus an explicit "none").
MAPPING_OPTIONS: dict[str, str] = {p.name: p.description for p in TARGET_PROPERTIES} | {
    "none": "No matching property; the column should not be imported",
}

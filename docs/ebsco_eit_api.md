# EBSCO EIT (EBSCOhost Integration Toolkit) API

## Credentials (from JHU Library, April 2024)
- Profile ID:       eitws2
- Profile Password: ebs8451
- Database:         bth (Business Source Complete)

## Status
The EIT REST endpoint requires the profile in 3-part format: `<account_id>.<group>.eitws2`
JHU's EBSCO account ID prefix is unknown — the library provided only the profile name.
Contact JHU library IT to obtain the full qualified profile ID (e.g., `s1234567.main.eitws2`).

## REST Endpoint
```
GET http://eit.ebscohost.com/Services/SearchService.asmx/Search
  ?prof=<account>.main.eitws2
  &pwd=ebs8451
  &authType=profile
  &query=<search+terms>
  &db=bth
  &numrec=10
  &format=detailed     # or 'full' for abody (full article text)
  &startrec=1
```

## SOAP Endpoint
```
POST http://eit.ebscohost.com/Services/SearchService.asmx
Content-Type: text/xml; charset=utf-8
SOAPAction: http://epnet.com/webservices/SearchService/2007/07/Search

Namespace: http://epnet.com/webservices/SearchService/2007/07/

SOAP Header: <tns:AuthorizationHeader>
  <tns:Profile><account>.main.eitws2</tns:Profile>
  <tns:Password>ebs8451</tns:Password>
  <tns:AuthType>profile</tns:AuthType>
</tns:AuthorizationHeader>
```

## Response Fields (format=detailed)
- `atl` — Article title
- `aug` — Author(s)
- `ab`  — Abstract
- `abody` — Full article text (format=full only)
- `pubinfo` — Journal, volume, pages, date
- `pdfLink` — Direct PDF URL
- `plink` — Persistent EBSCOhost link
- `su` — Subject/keywords
- `recordID` — Unique record ID

## WSDL
http://eit.ebscohost.com/Services/SearchService.asmx?WSDL
Full WSDL saved to: docs/ebsco_eit_wsdl.xml

## EDS API (different, requires separate provisioning)
- Auth: POST https://eds-api.ebscohost.com/authservice/rest/uidauth
- Session: GET https://eds-api.ebscohost.com/edsapi/rest/createsession
- Search: GET https://eds-api.ebscohost.com/edsapi/rest/search
- Requires EDS API profile provisioned in EBSCOadmin (separate from EIT)

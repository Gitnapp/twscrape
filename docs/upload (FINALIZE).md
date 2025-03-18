POST media/upload (FINALIZE)
Overview
The FINALIZE command should be called after the entire media file is uploaded using APPEND commands. If and (only if) the response of the FINALIZE command contains a processing_info field, it may also be necessary to use a STATUS command and wait for it to return success before proceeding to Tweet creation.

Request
Requests should be either multipart/form-data or application/x-www-form-urlencoded POST formats.

Note: The domain for this endpoint is upload.twitter.com

Response
The response provides a media identifier in the media_id (64-bit integer) and media_id_string (string) fields. Use the media_id_string provided in the API response from JavaScript and other languages that cannot accurately represent a long integer.

The returned mediaId is only valid for expires_after_secs seconds. Any attempt to use mediaId after this time period in other API calls will result in a Bad Request (HTTP 4xx) response.

If the response contains a processing_info field, then use the STATUS command to poll for the status of the FINALIZE operation. The async finalize approach is used for cases where media processing requires more time. In future, all video and animated GIF processing will only be supported using async finalize. This behavior is enabled if an upload session was initialized with a media_category parameter, and when then media type is either video or animated GIF.

If a processing_info field is NOT returned in the response, then media_id is ready for use in other API endpoints.

Resource Information
Response formats	JSON
Requires authentication?	Yes (user context only)
Rate limited?	Yes
Parameters
Name	Required	Description	Default Value	Example
command	required	Must be set to FINALIZE (case sensitive).		
media_id	required	The media_id returned from the INIT command.		
Example request
POST https://upload.twitter.com/1.1/media/upload.json?command=FINALIZE&media_id=710511363345354753

Example Result
// Example of sync FINALIZE response
{
  "media_id": 710511363345354753,
  "media_id_string": "710511363345354753",
  "size": 11065,
  "expires_after_secs": 86400,
  "video": {
    "video_type": "video/mp4"
  }
}

// Example of async FINALIZE response which requires further STATUS command call(s)
{
  "media_id": 710511363345354753,
  "media_id_string": "710511363345354753",
  "expires_after_secs": 86400,
  "size": 10240,
  "processing_info": {
    "state": "pending",
    "check_after_secs": 5 // check after 5 seconds for update using STATUS command
  }
}
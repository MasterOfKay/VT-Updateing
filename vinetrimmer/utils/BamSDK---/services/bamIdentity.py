from requests import Request

from vinetrimmer.utils.BamSDK.services import Service


# noinspection PyPep8Naming
class bamIdentity(Service):
    def identityLogin(self, email, password, access_token):
        # check login status
        endpoint = self.client.endpoints["check"]
        req = Request(
            method=endpoint.method,
            url=endpoint.href,
            headers=endpoint.get_headers(accessToken=access_token),
            json={
                "email": email,
            }
        ).prepare()
        res = self.session.send(req).json()

        next_step = res["operations"][0]
        if next_step.lower() == "otp":
            # OTP Login mode
            endpoint = self.client.endpoints['otpRequest']
            req = Request(
                method=endpoint.method,
                url=endpoint.href,
                headers=endpoint.get_headers(accessToken=access_token),
                json={
                    "email": email,
                    'reason': 'Login',
                }
            ).prepare()
            res = self.session.send(req)
            otp_code = input(f"Enter the OTP code that was sent to your email '{email}': ")

            endpoint = self.client.endpoints['otpRedeem']
            req = Request(
                method=endpoint.method,
                url=endpoint.href,
                headers=endpoint.get_headers(accessToken=access_token),
                json={
                    "email": email,
                    "passcode": otp_code
                }
            ).prepare()
            res = self.session.send(req)
            return res.json()
        else:
            # Default Login mode
            endpoint = self.client.endpoints["identityLogin"]
            req = Request(
                method=endpoint.method,
                url=endpoint.href,
                headers=endpoint.get_headers(accessToken=access_token),
                json={
                    "email": email,
                    "password": password
                }
            ).prepare()
            res = self.session.send(req)
            return res.json()

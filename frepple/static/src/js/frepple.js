/** @odoo-module **/

import { registry } from "@web/core/registry";
import { Component, xml, onWillStart } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";



class Quotes extends Component {
  setup() {
    this.orm = useService("orm");
    onWillStart(async () => {
      this.freppleURL = await this.orm.call(
        "res.company", "getFreppleURL", [false, '/quote/']
      );
    });
  }

  static template = xml`<iframe t-att-src="freppleURL" width="100%"
     height="100%" marginwidth="0" marginheight="0" frameborder="no"
     scrolling="yes" style="border-width:0px;"/>`;
}
registry.category("actions").add('frepple.quotes', Quotes);


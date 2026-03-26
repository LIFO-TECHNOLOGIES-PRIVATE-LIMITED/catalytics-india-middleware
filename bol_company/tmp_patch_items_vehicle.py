from pathlib import Path
import re

path = Path(r"C:\Github\catalytics-india-backend\import\services\tally_dc_simple.py")
text = path.read_text(encoding="utf-8")
line_ending = "\r\n" if "\r\n" in text else "\n"
text = text.replace("\r\n", "\n")

# Normalize and ensure comma after vehicle_for_dc
text = text.replace("'vehicle': vehicle_for_dc\n", "'vehicle': vehicle_for_dc,\n")

create_vehicle_block = (
    "            # Vehicle / challan type\n"
    "            vehicle_no = (voucher.get('DISPATCHEDTHROUGH') or voucher.get('MOTORVEHICLENO') or '').strip()\n"
    "            terms_of_delivery = (voucher.get('TERMSOFDELIVERY') or '').strip().lower()\n\n"
    "            if 'supplier' in terms_of_delivery:\n"
    "                challan_type = 3\n"
    "            elif 'trader' in terms_of_delivery:\n"
    "                challan_type = 4\n"
    "            elif 'delivery' in terms_of_delivery:\n"
    "                challan_type = 2\n"
    "            elif 'pickup' in terms_of_delivery or 'self' in terms_of_delivery:\n"
    "                challan_type = 1\n"
    "            else:\n"
    "                challan_type = 2 if vehicle_no else 1\n\n"
    "            vehicle_for_dc = None\n"
    "            if vehicle_no:\n"
    "                if challan_type == 2:\n"
    "                    try:\n"
    "                        vehicle_obj = dc_sync._get_or_create_vehicle(vehicle_no)\n"
    "                    except Exception:\n"
    "                        vehicle_obj = None\n"
    "                    if vehicle_obj:\n"
    "                        vehicle_for_dc = getattr(vehicle_obj, 'vehicle_no', None) or vehicle_no\n"
    "                    else:\n"
    "                        vehicle_for_dc = vehicle_no\n"
    "                else:\n"
    "                    vehicle_for_dc = vehicle_no\n\n"
)

update_vehicle_block = (
    "            # Vehicle / challan type\n"
    "            vehicle_no = (voucher.get('DISPATCHEDTHROUGH') or voucher.get('MOTORVEHICLENO') or '').strip()\n"
    "            terms_of_delivery = (voucher.get('TERMSOFDELIVERY') or '').strip().lower()\n\n"
    "            if 'supplier' in terms_of_delivery:\n"
    "                challan_type = 3\n"
    "            elif 'trader' in terms_of_delivery:\n"
    "                challan_type = 4\n"
    "            elif 'delivery' in terms_of_delivery:\n"
    "                challan_type = 2\n"
    "            elif 'pickup' in terms_of_delivery or 'self' in terms_of_delivery:\n"
    "                challan_type = 1\n"
    "            else:\n"
    "                challan_type = 2 if vehicle_no else 1\n\n"
    "            vehicle_for_dc = None\n"
    "            if vehicle_no:\n"
    "                if challan_type == 2:\n"
    "                    try:\n"
    "                        vehicle_obj = dc_sync._get_or_create_vehicle(vehicle_no)\n"
    "                    except Exception:\n"
    "                        vehicle_obj = None\n"
    "                    if vehicle_obj:\n"
    "                        vehicle_for_dc = getattr(vehicle_obj, 'vehicle_no', None) or vehicle_no\n"
    "                    else:\n"
    "                        vehicle_for_dc = vehicle_no\n"
    "                else:\n"
    "                    vehicle_for_dc = vehicle_no\n\n"
)

# Replace create vehicle block
text, count = re.subn(
    r"(?s)\n\s*# Vehicle / challan type\n.*?# Prepare DC creation kwargs\n",
    "\n" + create_vehicle_block + "            # Prepare DC creation kwargs\n",
    text,
    count=1,
)
if count == 0:
    raise SystemExit("Create vehicle block not found")

# Replace update vehicle block
text, count = re.subn(
    r"(?s)\n\s*# Vehicle / challan type\n.*?# Update DC header\n",
    "\n" + update_vehicle_block + "            # Update DC header\n",
    text,
    count=1,
)
if count == 0:
    raise SystemExit("Update vehicle block not found")

# Replace create items block
create_items_block = (
    "            # Create DC line items (with tax calculation)\n"
    "            total_qty = 0.0\n"
    "            challan_total = 0.0\n"
    "            for item in matched_items:\n"
    "                qty = item['quantity'] or 0.0\n"
    "                rate = item['rate'] or 0.0\n"
    "                amount = item['amount'] or 0.0\n\n"
    "                tax_percentage = 0.0\n"
    "                tax_price = 0.0\n"
    "                tax_names = []\n"
    "                taxable_amount = amount\n"
    "                gross_price = amount\n\n"
    "                product_tax = ProductTax.objects.filter(product=item['product']).first()\n"
    "                if product_tax and product_tax.tax_data:\n"
    "                    selected_tax_codes = []\n"
    "                    if billing_addr and billing_addr.selected_taxes:\n"
    "                        for tax_obj in billing_addr.selected_taxes:\n"
    "                            if isinstance(tax_obj, dict):\n"
    "                                tax_code = (tax_obj.get('tax_code') or '').upper()\n"
    "                                if tax_code:\n"
    "                                    selected_tax_codes.append(tax_code)\n"
    "                            elif isinstance(tax_obj, int):\n"
    "                                tax_record = Taxes.objects.filter(id=tax_obj).exclude(status=3).first()\n"
    "                                if tax_record:\n"
    "                                    selected_tax_codes.append(tax_record.code.upper())\n\n"
    "                    use_igst = 'IGST' in selected_tax_codes\n"
    "                    igst_rate = 0.0\n"
    "                    cgst_rate = 0.0\n"
    "                    sgst_rate = 0.0\n"
    "                    for tax_item in product_tax.tax_data:\n"
    "                        tax_code = (tax_item.get('tax_code') or '').upper()\n"
    "                        percentage = float(tax_item.get('percentage') or 0)\n"
    "                        if 'IGST' in tax_code:\n"
    "                            igst_rate = percentage\n"
    "                        elif 'CGST' in tax_code:\n"
    "                            cgst_rate = percentage\n"
    "                        elif 'SGST' in tax_code:\n"
    "                            sgst_rate = percentage\n\n"
    "                    if use_igst and igst_rate > 0:\n"
    "                        tax_percentage = igst_rate\n"
    "                        tax_names = [f\"IGST ({igst_rate}%)\"]\n"
    "                    elif ('CGST' in selected_tax_codes or 'SGST' in selected_tax_codes or not selected_tax_codes) and (cgst_rate > 0 or sgst_rate > 0):\n"
    "                        tax_percentage = cgst_rate + sgst_rate\n"
    "                        if cgst_rate > 0:\n"
    "                            tax_names.append(f\"CGST ({cgst_rate}%)\")\n"
    "                        if sgst_rate > 0:\n"
    "                            tax_names.append(f\"SGST ({sgst_rate}%)\")\n\n"
    "                    if tax_percentage > 0:\n"
    "                        tax_price = round(taxable_amount * (tax_percentage / 100), 2)\n"
    "                        gross_price = round(taxable_amount + tax_price, 2)\n\n"
    "                unit_price = item['product'].default_price if item['product'].default_price else (amount / qty if qty > 0 else rate)\n\n"
    "                OrderDetails.objects.create(\n"
    "                    delivery=dc,\n"
    "                    product=item['product'],\n"
    "                    quantity=qty,\n"
    "                    product_price=unit_price,\n"
    "                    total_price=gross_price,\n"
    "                    gross_price=gross_price,\n"
    "                    tax_percentage=tax_percentage,\n"
    "                    tax_price=tax_price,\n"
    "                    taxable_amount=taxable_amount,\n"
    "                    tax_names=tax_names,\n"
    "                )\n"
    "                total_qty += qty or 0.0\n"
    "                challan_total += gross_price or 0.0\n\n"
    "            update_fields = ['total_quantity', 'challan_total_amount']\n"
    "            dc.total_quantity = int(round(total_qty))\n"
    "            dc.challan_total_amount = challan_total\n"
    "            if hasattr(dc, 'challan_total_amount_wt_charges'):\n"
    "                dc.challan_total_amount_wt_charges = challan_total\n"
    "                update_fields.append('challan_total_amount_wt_charges')\n"
    "            dc.save(update_fields=update_fields)\n"
)
text, count = re.subn(
    r"(?s)\n\s*# Create DC line items\n.*?(?=\n\s*logger.info\(f\"DC created)",
    "\n" + create_items_block,
    text,
    count=1,
)
if count == 0:
    raise SystemExit("Create items block not found")

# Replace update items block
update_items_block = (
    "            # Create new details (with tax calculation)\n"
    "            total_qty = 0.0\n"
    "            challan_total = 0.0\n"
    "            for item in matched_items:\n"
    "                qty = item['quantity'] or 0.0\n"
    "                rate = item['rate'] or 0.0\n"
    "                amount = item['amount'] or 0.0\n\n"
    "                tax_percentage = 0.0\n"
    "                tax_price = 0.0\n"
    "                tax_names = []\n"
    "                taxable_amount = amount\n"
    "                gross_price = amount\n\n"
    "                product_tax = ProductTax.objects.filter(product=item['product']).first()\n"
    "                if product_tax and product_tax.tax_data:\n"
    "                    selected_tax_codes = []\n"
    "                    if billing_addr and billing_addr.selected_taxes:\n"
    "                        for tax_obj in billing_addr.selected_taxes:\n"
    "                            if isinstance(tax_obj, dict):\n"
    "                                tax_code = (tax_obj.get('tax_code') or '').upper()\n"
    "                                if tax_code:\n"
    "                                    selected_tax_codes.append(tax_code)\n"
    "                            elif isinstance(tax_obj, int):\n"
    "                                tax_record = Taxes.objects.filter(id=tax_obj).exclude(status=3).first()\n"
    "                                if tax_record:\n"
    "                                    selected_tax_codes.append(tax_record.code.upper())\n\n"
    "                    use_igst = 'IGST' in selected_tax_codes\n"
    "                    igst_rate = 0.0\n"
    "                    cgst_rate = 0.0\n"
    "                    sgst_rate = 0.0\n"
    "                    for tax_item in product_tax.tax_data:\n"
    "                        tax_code = (tax_item.get('tax_code') or '').upper()\n"
    "                        percentage = float(tax_item.get('percentage') or 0)\n"
    "                        if 'IGST' in tax_code:\n"
    "                            igst_rate = percentage\n"
    "                        elif 'CGST' in tax_code:\n"
    "                            cgst_rate = percentage\n"
    "                        elif 'SGST' in tax_code:\n"
    "                            sgst_rate = percentage\n\n"
    "                    if use_igst and igst_rate > 0:\n"
    "                        tax_percentage = igst_rate\n"
    "                        tax_names = [f\"IGST ({igst_rate}%)\"]\n"
    "                    elif ('CGST' in selected_tax_codes or 'SGST' in selected_tax_codes or not selected_tax_codes) and (cgst_rate > 0 or sgst_rate > 0):\n"
    "                        tax_percentage = cgst_rate + sgst_rate\n"
    "                        if cgst_rate > 0:\n"
    "                            tax_names.append(f\"CGST ({cgst_rate}%)\")\n"
    "                        if sgst_rate > 0:\n"
    "                            tax_names.append(f\"SGST ({sgst_rate}%)\")\n\n"
    "                    if tax_percentage > 0:\n"
    "                        tax_price = round(taxable_amount * (tax_percentage / 100), 2)\n"
    "                        gross_price = round(taxable_amount + tax_price, 2)\n\n"
    "                unit_price = item['product'].default_price if item['product'].default_price else (amount / qty if qty > 0 else rate)\n\n"
    "                OrderDetails.objects.create(\n"
    "                    delivery=existing_dc,\n"
    "                    product=item['product'],\n"
    "                    quantity=qty,\n"
    "                    product_price=unit_price,\n"
    "                    total_price=gross_price,\n"
    "                    gross_price=gross_price,\n"
    "                    tax_percentage=tax_percentage,\n"
    "                    tax_price=tax_price,\n"
    "                    taxable_amount=taxable_amount,\n"
    "                    tax_names=tax_names,\n"
    "                )\n"
    "                total_qty += qty or 0.0\n"
    "                challan_total += gross_price or 0.0\n\n"
    "            update_fields = ['total_quantity', 'challan_total_amount']\n"
    "            existing_dc.total_quantity = int(round(total_qty))\n"
    "            existing_dc.challan_total_amount = challan_total\n"
    "            if hasattr(existing_dc, 'challan_total_amount_wt_charges'):\n"
    "                existing_dc.challan_total_amount_wt_charges = challan_total\n"
    "                update_fields.append('challan_total_amount_wt_charges')\n"
    "            existing_dc.save(update_fields=update_fields)\n"
)
text, count = re.subn(
    r"(?s)\n\s*# Create new details\n.*?(?=\n\s*logger.info\(f\"DC updated)",
    "\n" + update_items_block,
    text,
    count=1,
)
if count == 0:
    raise SystemExit("Update items block not found")

# Remove any leftover "# Calculate totals" blocks if present
text = re.sub(r"\n\s*# Calculate totals\n.*?\n", "\n", text)

path.write_text(text.replace("\n", line_ending), encoding="utf-8")
print("Patched items + vehicle blocks")

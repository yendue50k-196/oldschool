from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
import json
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from apps.accounts.models import User
from apps.accounts.permissions import assert_can_manage_imports, assert_roles
from apps.branches.models import CoSo
from apps.catalog.models import BienThe
from apps.inventory.models import StockMovement, TonKho
from apps.inventory.services import StockDeltaSpec, apply_stock_movements
from apps.notifications.models import ThongBao
from apps.notifications.services import notify_admins, notify_branch_managers, notify_users
from apps.purchasing.models import ChiTietPhieuNhapKho, NhaCungCap, PhieuNhapKho

RESTOCK_REQUEST_ENTITY = "StockRestockRequest"
RESTOCK_REQUEST_ACK_ENTITY = "StockRestockRequestAck"


@dataclass
class ImportLineSpec:
    variant_id: int
    so_luong: int
    don_gia: Decimal
    chiet_khau: Decimal = Decimal("0")
    ghi_chu: str = ""


@transaction.atomic
def send_stock_restock_request(
    *,
    actor: User,
    rows: list[tuple[str, str]],
    reason: str,
) -> int:
    actor = User.objects.select_for_update().select_related("branch").get(pk=actor.pk)
    assert_roles(actor, User.Role.MANAGER)
    if not actor.branch or actor.branch.ma_co_so.upper() != "CS1":
        raise PermissionDenied("Chỉ Quản lý kho CS1 mới được gửi đề nghị nhập hàng.")

    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("Vui lòng nhập lý do đề nghị.")
    if len(reason) > 1000:
        raise ValidationError("Lý do đề nghị không được vượt quá 1000 ký tự.")

    selected_rows = []
    seen_variant_ids = set()
    for index, (raw_variant_id, raw_quantity) in enumerate(rows, start=1):
        raw_variant_id = str(raw_variant_id or "").strip()
        raw_quantity = str(raw_quantity or "").strip()
        if not raw_variant_id and not raw_quantity:
            continue
        if not raw_variant_id or not raw_quantity:
            raise ValidationError(f"Dòng {index}: Vui lòng chọn SKU và nhập số lượng đề nghị.")
        if not raw_variant_id.isascii() or not raw_variant_id.isdecimal():
            raise ValidationError(f"Dòng {index}: SKU không hợp lệ.")
        if not raw_quantity.isascii() or not raw_quantity.isdecimal():
            raise ValidationError(f"Dòng {index}: Số lượng phải là số nguyên lớn hơn 0.")

        variant_id = int(raw_variant_id)
        quantity = int(raw_quantity)
        if quantity <= 0:
            raise ValidationError(f"Dòng {index}: Số lượng phải lớn hơn 0.")
        if variant_id in seen_variant_ids:
            raise ValidationError(f"Dòng {index}: Không được chọn trùng SKU.")
        seen_variant_ids.add(variant_id)
        selected_rows.append((variant_id, quantity))

    if not selected_rows:
        raise ValidationError("Vui lòng chọn ít nhất một SKU để đề nghị.")

    variants = {
        variant.id: variant
        for variant in BienThe.objects.select_related("san_pham").filter(
            id__in=[variant_id for variant_id, _ in selected_rows],
            trang_thai=BienThe.Status.ACTIVE,
            san_pham__trang_thai="ACTIVE",
        )
    }
    if len(variants) != len(selected_rows):
        raise ValidationError("Có SKU không tồn tại hoặc đã ngừng hoạt động. Vui lòng kiểm tra lại.")

    branch = actor.branch
    balances = {
        balance.bien_the_id: balance.so_luong
        for balance in TonKho.objects.select_for_update().filter(
            co_so=branch,
            bien_the_id__in=variants,
        )
    }
    now = timezone.now()
    recent_cutoff = now - timedelta(minutes=5)
    recent_requests = ThongBao.objects.filter(
        loai_thong_bao=ThongBao.NotificationType.SYSTEM,
        related_entity_type=RESTOCK_REQUEST_ENTITY,
        created_at__gte=recent_cutoff,
    ).only("noi_dung")
    recently_requested_variant_ids = set()
    for notification in recent_requests:
        try:
            request_data = json.loads(notification.noi_dung)
        except (TypeError, json.JSONDecodeError):
            continue
        if request_data.get("sender_id") == actor.id:
            recently_requested_variant_ids.update(item["variant_id"] for item in request_data.get("items", []))
    for variant_id, _ in selected_rows:
        if variant_id in recently_requested_variant_ids:
            raise ValidationError(
                f"Bạn đã gửi đề nghị cho SKU {variants[variant_id].ma_sku} trong vòng 5 phút."
            )

    request_data = {
        "sender_id": actor.id,
        "sender_name": actor.display_name,
        "sender_username": actor.username,
        "branch_id": branch.id,
        "branch_name": branch.ten_co_so,
        "reason": reason,
        "sent_at": now.isoformat(),
        "items": [
            {
                "variant_id": variant_id,
                "sku": variants[variant_id].ma_sku,
                "product_name": variants[variant_id].san_pham.ten_hang_hoa,
                "color": variants[variant_id].mau_sac,
                "size": variants[variant_id].kich_co,
                "stock": balances.get(variant_id, 0),
                "quantity": quantity,
            }
            for variant_id, quantity in selected_rows
        ],
    }
    notifications = notify_admins(
        notification_type=ThongBao.NotificationType.SYSTEM,
        title=f"Đề nghị nhập hàng CS1 ({len(selected_rows)} SKU)",
        content=json.dumps(request_data, ensure_ascii=False),
        branch=branch,
        related_entity_type=RESTOCK_REQUEST_ENTITY,
        related_entity_id=actor.id,
    )
    if not notifications:
        raise ValidationError("Không tìm thấy tài khoản ADMIN đang hoạt động để nhận đề nghị.")

    request_id = notifications[0].id
    detail_url = reverse("purchasing:stock_restock_request_detail", args=[request_id])
    ThongBao.objects.filter(pk__in=[notification.pk for notification in notifications]).update(
        related_entity_id=request_id,
        target_url=detail_url,
    )

    return len(selected_rows)


@transaction.atomic
def acknowledge_stock_restock_request(*, notification_id: int, admin: User) -> dict | None:
    assert_roles(admin, User.Role.ADMIN)
    notification = ThongBao.objects.filter(
        pk=notification_id,
        nguoi_nhan=admin,
        loai_thong_bao=ThongBao.NotificationType.SYSTEM,
        related_entity_type=RESTOCK_REQUEST_ENTITY,
    ).first()
    if not notification:
        return None

    request_id = notification.related_entity_id
    if not request_id:
        raise ValidationError("Thông báo đề nghị nhập hàng không hợp lệ.")
    canonical_notification = (
        ThongBao.objects.select_for_update()
        .filter(
            pk=request_id,
            loai_thong_bao=ThongBao.NotificationType.SYSTEM,
            related_entity_type=RESTOCK_REQUEST_ENTITY,
            related_entity_id=request_id,
        )
        .first()
    )
    if not canonical_notification:
        raise ValidationError("Không tìm thấy lô đề nghị nhập hàng.")

    try:
        request_data = json.loads(canonical_notification.noi_dung)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValidationError("Dữ liệu đề nghị nhập hàng không hợp lệ.") from exc

    if not notification.da_doc:
        ThongBao.objects.filter(
            loai_thong_bao=ThongBao.NotificationType.SYSTEM,
            related_entity_type=RESTOCK_REQUEST_ENTITY,
            related_entity_id=request_id,
            nguoi_nhan=admin,
            da_doc=False,
        ).update(da_doc=True)
        if not ThongBao.objects.filter(
            loai_thong_bao=ThongBao.NotificationType.SYSTEM,
            related_entity_type=RESTOCK_REQUEST_ACK_ENTITY,
            related_entity_id=request_id,
        ).exists():
            sender = User.objects.filter(
                pk=request_data.get("sender_id"),
                role=User.Role.MANAGER,
                branch__ma_co_so__iexact="CS1",
            ).first()
            if sender:
                notify_users(
                    users=[sender],
                    notification_type=ThongBao.NotificationType.SYSTEM,
                    title="Chủ cửa hàng đã nhận đề nghị nhập hàng",
                    content=(
                        f"Chủ cửa hàng đã xem và nhận đề nghị nhập hàng gồm "
                        f"{len(request_data.get('items', []))} SKU bạn gửi lúc "
                        f"{timezone.localtime(datetime.fromisoformat(request_data['sent_at'])):%d/%m/%Y %H:%M}. "
                        "Đây là xác nhận đã nhận thông tin, không phải phê duyệt nhập hàng."
                    ),
                    branch=sender.branch,
                    related_entity_type=RESTOCK_REQUEST_ACK_ENTITY,
                    related_entity_id=request_id,
                    target_url=reverse("notifications:list"),
                )

    return request_data


def generate_next_import_code() -> str:
    prefix = f"PNK-{timezone.localdate().strftime('%Y%m%d')}-"
    existing_codes = PhieuNhapKho.objects.filter(ma_phieu_nhap_kho__startswith=prefix).values_list(
        "ma_phieu_nhap_kho", flat=True
    )
    max_seq = 0
    for code in existing_codes:
        suffix = code[len(prefix):]
        if suffix.isdigit():
            max_seq = max(max_seq, int(suffix))
    return f"{prefix}{max_seq + 1:03d}"


def create_supplier(*, actor: User, ma_nha_cung_cap: str, ten_nha_cung_cap: str, dia_chi: str, sdt: str) -> NhaCungCap:
    assert_can_manage_imports(actor)
    code = ma_nha_cung_cap.strip().upper()
    if not code or not ten_nha_cung_cap.strip() or not dia_chi.strip() or not sdt.strip():
        raise ValidationError("Vui lòng nhập đầy đủ Mã nhà cung cấp, Tên nhà cung cấp, Địa chỉ và Số điện thoại.")
    if NhaCungCap.objects.filter(ma_nha_cung_cap__iexact=code).exists():
        raise ValidationError({"ma_nha_cung_cap": f"Mã nhà cung cấp '{code}' đã tồn tại trong hệ thống."})
    return NhaCungCap.objects.create(
        ma_nha_cung_cap=code,
        ten_nha_cung_cap=ten_nha_cung_cap.strip(),
        dia_chi=dia_chi.strip(),
        sdt=sdt.strip(),
    )


def _validate_and_build_lines(line_specs: list[ImportLineSpec]) -> tuple[list[dict], Decimal]:
    if not line_specs:
        raise ValidationError("Phiếu nhập kho phải có ít nhất 1 dòng hàng hóa.")
    seen_variants = set()
    validated_lines = []
    grand_total = Decimal("0")

    for idx, spec in enumerate(line_specs, start=1):
        variant = BienThe.objects.select_related("san_pham").filter(id=spec.variant_id).first()
        if not variant or variant.san_pham.trang_thai != "ACTIVE" or variant.trang_thai != "ACTIVE":
            raise ValidationError(f"Dòng {idx}: Mã hàng / SKU không tồn tại hoặc đã ngừng kinh doanh.")
        if variant.id in seen_variants:
            raise ValidationError(f"Dòng {idx}: Sản phẩm {variant.ma_sku} bị trùng lặp trên cùng một phiếu nhập.")
        seen_variants.add(variant.id)

        qty = int(spec.so_luong)
        if qty <= 0:
            raise ValidationError(f"Dòng {idx} ({variant.ma_sku}): Số lượng nhập phải lớn hơn 0.")
        price = Decimal(str(spec.don_gia))
        if price < 0:
            raise ValidationError(f"Dòng {idx} ({variant.ma_sku}): Đơn giá nhập không được âm.")
        disc = Decimal(str(spec.chiet_khau or "0"))
        if disc < 0 or disc > 100:
            raise ValidationError(f"Dòng {idx} ({variant.ma_sku}): Chiết khấu (%) phải từ 0% đến 100%.")
        gross = Decimal(qty) * price
        discount_amount = (gross * disc / Decimal("100")).quantize(Decimal("1"))
        line_total = gross - discount_amount
        grand_total += line_total
        validated_lines.append(
            {
                "variant": variant,
                "so_luong": qty,
                "don_gia": price,
                "chiet_khau": disc,
                "thanh_tien": line_total,
                "ghi_chu": (spec.ghi_chu or "").strip(),
            }
        )
    return validated_lines, grand_total


@transaction.atomic
def create_import_receipt(
    *,
    actor: User,
    nha_cung_cap: NhaCungCap,
    line_specs: list[ImportLineSpec],
    co_so_nhan: CoSo | None = None,
    ma_phieu_nhap_kho: str = "",
    ngay_nhap=None,
    ly_do_nhap: str = "Nhập hàng từ nhà cung cấp",
    ghi_chu: str = "",
    submit_for_approval: bool = True,
) -> PhieuNhapKho:
    target_branch = co_so_nhan or CoSo.get_central_warehouse()
    if not target_branch:
        raise ValidationError("Chưa cấu hình Cơ sở 1 (Kho tổng) để nhận hàng nhập kho.")
    assert_can_manage_imports(actor, target_branch)

    code = (ma_phieu_nhap_kho or "").strip().upper() or generate_next_import_code()
    if PhieuNhapKho.objects.filter(ma_phieu_nhap_kho__iexact=code).exists():
        raise ValidationError({"ma_phieu_nhap_kho": f"Mã phiếu nhập kho '{code}' đã tồn tại."})

    validated_lines, grand_total = _validate_and_build_lines(line_specs)
    status = PhieuNhapKho.Status.PENDING_APPROVAL if submit_for_approval else PhieuNhapKho.Status.DRAFT

    receipt = PhieuNhapKho.objects.create(
        ma_phieu_nhap_kho=code,
        nha_cung_cap=nha_cung_cap,
        co_so_nhan=target_branch,
        dia_chi=nha_cung_cap.dia_chi,
        sdt=nha_cung_cap.sdt,
        ngay_nhap=ngay_nhap or timezone.now(),
        ly_do_nhap=(ly_do_nhap or "Nhập hàng từ nhà cung cấp").strip(),
        tong_tien=grand_total,
        trang_thai=status,
        ghi_chu=(ghi_chu or "").strip(),
        nguoi_tao=actor,
    )

    for item in validated_lines:
        ChiTietPhieuNhapKho.objects.create(
            phieu_nhap_kho=receipt,
            bien_the=item["variant"],
            so_luong=item["so_luong"],
            don_gia=item["don_gia"],
            chiet_khau=item["chiet_khau"],
            thanh_tien=item["thanh_tien"],
            ghi_chu=item["ghi_chu"],
        )

    if status == PhieuNhapKho.Status.PENDING_APPROVAL:
        try:
            url = reverse("purchasing:import_detail", args=[receipt.id])
        except Exception:
            url = "/purchasing/"
        notify_admins(
            notification_type=ThongBao.NotificationType.IMPORT_SUBMITTED,
            title=f"Phiếu nhập kho mới chờ duyệt: {receipt.ma_phieu_nhap_kho}",
            content=(
                f"Người lập: {actor.display_name} ({target_branch.ten_co_so}) | "
                f"NCC: {nha_cung_cap.ten_nha_cung_cap} | Tổng tiền: {grand_total:,.0f} VNĐ."
            ),
            branch=target_branch,
            related_entity_type="PhieuNhapKho",
            related_entity_id=receipt.id,
            target_url=url,
        )

    return receipt


@transaction.atomic
def update_import_receipt(
    *,
    actor: User,
    receipt: PhieuNhapKho,
    nha_cung_cap: NhaCungCap,
    line_specs: list[ImportLineSpec],
    ly_do_nhap: str = "",
    ghi_chu: str = "",
    submit_for_approval: bool = True,
) -> PhieuNhapKho:
    receipt = PhieuNhapKho.objects.select_for_update().get(pk=receipt.pk)
    assert_can_manage_imports(actor, receipt.co_so_nhan)
    if not receipt.is_editable:
        raise ValidationError("Không thể chỉnh sửa phiếu nhập kho đã được phê duyệt.")

    validated_lines, grand_total = _validate_and_build_lines(line_specs)
    receipt.nha_cung_cap = nha_cung_cap
    receipt.dia_chi = nha_cung_cap.dia_chi
    receipt.sdt = nha_cung_cap.sdt
    if ly_do_nhap:
        receipt.ly_do_nhap = ly_do_nhap.strip()
    receipt.ghi_chu = (ghi_chu or "").strip()
    receipt.tong_tien = grand_total

    previous_status = receipt.trang_thai
    receipt.trang_thai = (
        PhieuNhapKho.Status.PENDING_APPROVAL
        if submit_for_approval
        else PhieuNhapKho.Status.DRAFT
    )
    receipt.save()

    receipt.lines.all().delete()
    for item in validated_lines:
        ChiTietPhieuNhapKho.objects.create(
            phieu_nhap_kho=receipt,
            bien_the=item["variant"],
            so_luong=item["so_luong"],
            don_gia=item["don_gia"],
            chiet_khau=item["chiet_khau"],
            thanh_tien=item["thanh_tien"],
            ghi_chu=item["ghi_chu"],
        )

    if submit_for_approval and previous_status != PhieuNhapKho.Status.PENDING_APPROVAL:
        try:
            target_url = reverse("purchasing:import_detail", args=[receipt.id])
        except Exception:
            target_url = "/purchasing/"
        notify_admins(
            notification_type=ThongBao.NotificationType.IMPORT_SUBMITTED,
            title=f"Phiếu nhập kho {receipt.ma_phieu_nhap_kho} đã được cập nhật & gửi duyệt",
            content=(
                f"Quản lý {actor.display_name} ({receipt.co_so_nhan.ten_co_so}) "
                f"đã cập nhật và gửi duyệt lại phiếu nhập {receipt.ma_phieu_nhap_kho} "
                f"(Tổng tiền: {int(receipt.tong_tien):,} đ)."
            ),
            branch=receipt.co_so_nhan,
            related_entity_type="PhieuNhapKho",
            related_entity_id=receipt.id,
            target_url=target_url,
        )

    return receipt


@transaction.atomic
def delete_import_receipt(*, actor: User, receipt: PhieuNhapKho) -> str:
    receipt = PhieuNhapKho.objects.select_for_update().get(pk=receipt.pk)
    assert_can_manage_imports(actor, receipt.co_so_nhan)
    if not receipt.is_deletable:
        raise ValidationError("Không thể xóa phiếu nhập kho đã được phê duyệt và ghi nhận tồn kho.")
    code = receipt.ma_phieu_nhap_kho
    receipt.delete()
    return code


@transaction.atomic
def approve_import_receipt(*, actor: User, receipt: PhieuNhapKho) -> PhieuNhapKho:
    assert_roles(actor, User.Role.ADMIN)
    receipt = (
        PhieuNhapKho.objects.select_for_update()
        .select_related("co_so_nhan", "nha_cung_cap")
        .get(pk=receipt.pk)
    )
    if receipt.trang_thai == PhieuNhapKho.Status.APPROVED:
        raise ValidationError(f"Phiếu nhập kho {receipt.ma_phieu_nhap_kho} đã được phê duyệt trước đó.")
    if receipt.trang_thai not in {PhieuNhapKho.Status.PENDING_APPROVAL, PhieuNhapKho.Status.DRAFT, PhieuNhapKho.Status.RETURNED}:
        raise ValidationError("Trạng thái phiếu nhập kho không hợp lệ để phê duyệt.")

    lines = list(receipt.lines.select_related("bien_the", "bien_the__san_pham").all())
    if not lines:
        raise ValidationError("Phiếu nhập kho không có chi tiết hàng hóa để phê duyệt.")

    now = timezone.now()
    deltas = [
        StockDeltaSpec(
            variant=line.bien_the,
            quantity_delta=line.so_luong,
            unit_price_snapshot=line.don_gia,
            source_line_id=line.id,
            idempotency_key=f"IMPORT_APPROVE:{receipt.id}:LINE:{line.id}",
            notes=f"Nhập kho theo phiếu {receipt.ma_phieu_nhap_kho} ({receipt.nha_cung_cap.ten_nha_cung_cap})",
        )
        for line in lines
    ]

    apply_stock_movements(
        branch=receipt.co_so_nhan,
        deltas=deltas,
        movement_type=StockMovement.MovementType.IMPORT_IN,
        source_doc_type="PhieuNhapKho",
        source_doc_id=receipt.id,
        source_doc_code=receipt.ma_phieu_nhap_kho,
        actor=actor,
        effective_at=now,
    )

    receipt.trang_thai = PhieuNhapKho.Status.APPROVED
    receipt.nguoi_duyet = actor
    receipt.thoi_gian_duyet = now
    receipt.ly_do_tra_lai = ""
    receipt.save(update_fields=["trang_thai", "nguoi_duyet", "thoi_gian_duyet", "ly_do_tra_lai", "updated_at"])

    try:
        url = reverse("purchasing:import_detail", args=[receipt.id])
    except Exception:
        url = "/purchasing/"
    notify_branch_managers(
        branch=receipt.co_so_nhan,
        notification_type=ThongBao.NotificationType.IMPORT_APPROVED,
        title=f"Đã duyệt phiếu nhập kho {receipt.ma_phieu_nhap_kho}",
        content=f"Quản trị viên {actor.display_name} đã phê duyệt phiếu nhập kho {receipt.ma_phieu_nhap_kho}. Tồn kho đã được cộng.",
        related_entity_type="PhieuNhapKho",
        related_entity_id=receipt.id,
        target_url=url,
    )

    return receipt


@transaction.atomic
def return_import_receipt(*, actor: User, receipt: PhieuNhapKho, reason: str = "") -> PhieuNhapKho:
    assert_roles(actor, User.Role.ADMIN)
    receipt = PhieuNhapKho.objects.select_for_update().get(pk=receipt.pk)
    if receipt.trang_thai == PhieuNhapKho.Status.APPROVED:
        raise ValidationError("Không thể trả lại phiếu nhập kho đã được phê duyệt.")
    if receipt.trang_thai == PhieuNhapKho.Status.RETURNED:
        raise ValidationError("Phiếu nhập kho này đã ở trạng thái trả lại chỉnh sửa.")
    clean_reason = (reason or "Cần kiểm tra và bổ sung lại thông tin chứng từ nhập kho.").strip()
    receipt.trang_thai = PhieuNhapKho.Status.RETURNED
    receipt.nguoi_duyet = actor
    receipt.thoi_gian_duyet = timezone.now()
    receipt.ly_do_tra_lai = clean_reason
    receipt.save(update_fields=["trang_thai", "nguoi_duyet", "thoi_gian_duyet", "ly_do_tra_lai", "updated_at"])

    try:
        url = reverse("purchasing:import_detail", args=[receipt.id])
    except Exception:
        url = "/purchasing/"
    notify_branch_managers(
        branch=receipt.co_so_nhan,
        notification_type=ThongBao.NotificationType.IMPORT_RETURNED,
        title=f"Yêu cầu chỉnh sửa phiếu nhập {receipt.ma_phieu_nhap_kho}",
        content=f"Phiếu nhập {receipt.ma_phieu_nhap_kho} được trả lại để chỉnh sửa. Lý do: {clean_reason}",
        related_entity_type="PhieuNhapKho",
        related_entity_id=receipt.id,
        target_url=url,
    )
    return receipt


@transaction.atomic
def propose_supplier_return(*, actor: User, receipt: PhieuNhapKho, reason: str) -> PhieuNhapKho:
    assert_can_manage_imports(actor, receipt.co_so_nhan)
    receipt = PhieuNhapKho.objects.select_for_update().get(pk=receipt.pk)
    if receipt.trang_thai == PhieuNhapKho.Status.APPROVED:
        raise ValidationError("Không thể báo trả hàng đối với lô hàng đã được phê duyệt nhập kho.")
    if not (reason or "").strip():
        raise ValidationError("Vui lòng nhập lý do hàng lỗi / không đạt yêu cầu kiểm hàng.")

    receipt.trang_thai = PhieuNhapKho.Status.RETURN_PROPOSED
    receipt.ly_do_loi_ncc = reason.strip()
    receipt.nguoi_bao_loi = actor
    receipt.thoi_gian_bao_loi = timezone.now()
    receipt.save(update_fields=["trang_thai", "ly_do_loi_ncc", "nguoi_bao_loi", "thoi_gian_bao_loi", "updated_at"])

    try:
        url = reverse("purchasing:import_detail", args=[receipt.id])
    except Exception:
        url = "/purchasing/"
    notify_admins(
        notification_type=ThongBao.NotificationType.IMPORT_RETURNED,
        title=f"Báo lỗi trả NCC lô hàng: {receipt.ma_phieu_nhap_kho}",
        content=f"Quản lý {actor.display_name} báo lỗi lô hàng {receipt.ma_phieu_nhap_kho} từ NCC {receipt.nha_cung_cap.ten_nha_cung_cap}. Lý do: {reason.strip()}",
        branch=receipt.co_so_nhan,
        related_entity_type="PhieuNhapKho",
        related_entity_id=receipt.id,
        target_url=url,
    )
    return receipt


@transaction.atomic
def confirm_supplier_return(*, actor: User, receipt: PhieuNhapKho, admin_note: str = "") -> PhieuNhapKho:
    assert_roles(actor, User.Role.ADMIN)
    receipt = PhieuNhapKho.objects.select_for_update().get(pk=receipt.pk)
    if receipt.trang_thai == PhieuNhapKho.Status.APPROVED:
        raise ValidationError("Không thể xác nhận trả hàng cho phiếu nhập đã phê duyệt nhập kho.")

    receipt.trang_thai = PhieuNhapKho.Status.RETURNED_TO_SUPPLIER
    receipt.nguoi_duyet = actor
    receipt.thoi_gian_duyet = timezone.now()
    if admin_note.strip():
        receipt.ghi_chu = f"{receipt.ghi_chu}\n[Chủ xác nhận trả NCC]: {admin_note.strip()}".strip()
    receipt.save(update_fields=["trang_thai", "nguoi_duyet", "thoi_gian_duyet", "ghi_chu", "updated_at"])

    try:
        url = reverse("purchasing:import_detail", args=[receipt.id])
    except Exception:
        url = "/purchasing/"
    notify_branch_managers(
        branch=receipt.co_so_nhan,
        notification_type=ThongBao.NotificationType.IMPORT_APPROVED,
        title=f"Đã xác nhận trả hàng NCC cho lô: {receipt.ma_phieu_nhap_kho}",
        content=f"Chủ cửa hàng {actor.display_name} đã xác nhận trả hàng về NCC {receipt.nha_cung_cap.ten_nha_cung_cap}. Lô hàng không nhập kho.",
        related_entity_type="PhieuNhapKho",
        related_entity_id=receipt.id,
        target_url=url,
    )
    return receipt

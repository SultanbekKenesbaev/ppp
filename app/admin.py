from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from . import db
from .models import District, Mahalla, Street, User, WorkerAssignment
from sqlalchemy import or_
from .utils import require_role, hash_password

bp = Blueprint("admin", __name__)

@bp.get("/structure")
@login_required
def structure():
    require_role(current_user, "admin")
    districts = District.query.order_by(District.name.asc()).all()
    mahallas = Mahalla.query.order_by(Mahalla.name.asc()).all()
    streets = Street.query.order_by(Street.name.asc()).all()
    return render_template("admin_structure.html", districts=districts, mahallas=mahallas, streets=streets)

@bp.post("/district/create")
@login_required
def district_create():
    require_role(current_user, "admin")
    name = request.form.get("name", "").strip()
    if not name:
        flash("Название района обязательно", "error")
        return redirect(url_for("admin.structure"))
    if District.query.filter_by(name=name).first():
        flash("Такой район уже существует", "error")
        return redirect(url_for("admin.structure"))
    db.session.add(District(name=name))
    db.session.commit()
    flash("Район добавлен", "ok")
    return redirect(url_for("admin.structure"))

@bp.post("/mahalla/create")
@login_required
def mahalla_create():
    require_role(current_user, "admin")
    name = request.form.get("name", "").strip()
    district_id = request.form.get("district_id", type=int)
    if not name or not district_id:
        flash("Заполните все поля", "error")
        return redirect(url_for("admin.structure"))
    db.session.add(Mahalla(name=name, district_id=district_id))
    db.session.commit()
    flash("Махалля добавлена", "ok")
    return redirect(url_for("admin.structure"))

@bp.post("/street/create")
@login_required
def street_create():
    require_role(current_user, "admin")
    name = request.form.get("name", "").strip()
    mahalla_id = request.form.get("mahalla_id", type=int)
    if not name or not mahalla_id:
        flash("Заполните все поля", "error")
        return redirect(url_for("admin.structure"))
    db.session.add(Street(name=name, mahalla_id=mahalla_id))
    db.session.commit()
    flash("Улица добавлена", "ok")
    return redirect(url_for("admin.structure"))

@bp.get("/workers")
@login_required
def workers():
    require_role(current_user, "admin")
    q = request.args.get("q", "").strip()
    workers_q = User.query.filter_by(role="worker", is_active=True)
    if q:
        like = f"%{q}%"
        workers_q = workers_q.filter(or_(
            User.first_name.ilike(like),
            User.last_name.ilike(like),
            User.middle_name.ilike(like),
            User.login.ilike(like),
            (User.last_name + " " + User.first_name + " " + User.middle_name).ilike(like)
        ))
    workers = workers_q.order_by(User.last_name.asc()).all()

    # свободные улицы: улицы без назначения
    assigned_street_ids = {a.street_id for a in WorkerAssignment.query.all()}
    free_streets = Street.query.order_by(Street.name.asc()).all()
    free_streets = [s for s in free_streets if s.id not in assigned_street_ids]

    # для отображения дерева у работника
    assignments = WorkerAssignment.query.all()
    by_worker = {a.worker_id: a for a in assignments}

    districts = District.query.order_by(District.name.asc()).all()
    mahallas = Mahalla.query.order_by(Mahalla.name.asc()).all()

    return render_template(
        "admin_workers.html",
        workers=workers,
        free_streets=free_streets,
        by_worker=by_worker,
        districts=districts,
        mahallas=mahallas,
        q=q,
    )

@bp.post("/worker/create")
@login_required
def worker_create():
    require_role(current_user, "admin")

    street_id = request.form.get("street_id", type=int)
    login = request.form.get("login", "").strip()
    password = request.form.get("password", "").strip()
    first_name = request.form.get("first_name", "").strip()
    last_name = request.form.get("last_name", "").strip()
    middle_name = request.form.get("middle_name", "").strip()
    note = request.form.get("note", "").strip()

    if not all([street_id, login, password, first_name, last_name]):
        flash("Заполните обязательные поля", "error")
        return redirect(url_for("admin.workers"))

    if User.query.filter_by(login=login).first():
        flash("Логин занят", "error")
        return redirect(url_for("admin.workers"))

    # улица должна быть свободна
    if WorkerAssignment.query.filter_by(street_id=street_id).first():
        flash("На этой улице уже есть работник", "error")
        return redirect(url_for("admin.workers"))

    user = User(
        role="worker",
        login=login,
        password_hash=hash_password(password),
        first_name=first_name,
        last_name=last_name,
        middle_name=middle_name,
        note=note,
    )
    db.session.add(user)
    db.session.flush()

    db.session.add(WorkerAssignment(street_id=street_id, worker_id=user.id))
    db.session.commit()

    flash("Работник создан", "ok")
    return redirect(url_for("admin.workers"))

@bp.post("/worker/update/<int:worker_id>")
@login_required
def worker_update(worker_id: int):
    require_role(current_user, "admin")

    user = User.query.filter_by(id=worker_id, role="worker").first_or_404()

    user.first_name = request.form.get("first_name", user.first_name).strip()
    user.last_name = request.form.get("last_name", user.last_name).strip()
    user.middle_name = request.form.get("middle_name", user.middle_name).strip()
    user.note = request.form.get("note", user.note).strip()

    new_pass = request.form.get("password", "").strip()
    if new_pass:
        user.password_hash = hash_password(new_pass)

    db.session.add(user)
    db.session.commit()
    flash("Работник обновлён", "ok")
    return redirect(url_for("admin.workers"))



@bp.post("/worker/delete/<int:worker_id>")
@login_required
def worker_delete(worker_id: int):
    require_role(current_user, "admin")

    user = User.query.filter_by(id=worker_id, role="worker").first_or_404()

    # удалить назначение улицы
    a = WorkerAssignment.query.filter_by(worker_id=user.id).first()
    if a:
        db.session.delete(a)

    # мягкое удаление: скрываем из списка и отключаем вход
    user.is_active = False
    # освобождаем логин для повторного использования
    user.login = f"{user.login}__deleted__{user.id}"
    db.session.add(user)
    db.session.commit()
    flash("Работник удалён", "ok")
    return redirect(url_for("admin.workers"))


# =========================
# UPDATE / DELETE: District
# =========================

@bp.post("/district/update/<int:district_id>")
@login_required
def district_update(district_id: int):
    require_role(current_user, "admin")
    d = District.query.get_or_404(district_id)
    name = request.form.get("name", "").strip()
    if not name:
        flash("Название района не может быть пустым", "error")
        return redirect(url_for("admin.structure"))
    # уникальность
    exists = District.query.filter(District.name == name, District.id != d.id).first()
    if exists:
        flash("Район с таким названием уже существует", "error")
        return redirect(url_for("admin.structure"))
    d.name = name
    db.session.commit()
    flash("Район обновлён", "ok")
    return redirect(url_for("admin.structure"))

@bp.post("/district/delete/<int:district_id>")
@login_required
def district_delete(district_id: int):
    require_role(current_user, "admin")
    d = District.query.get_or_404(district_id)

    # если есть махалли — нельзя
    if Mahalla.query.filter_by(district_id=d.id).count() > 0:
        flash("Нельзя удалить район: в нём есть махалли", "error")
        return redirect(url_for("admin.structure"))

    db.session.delete(d)
    db.session.commit()
    flash("Район удалён", "ok")
    return redirect(url_for("admin.structure"))


# =========================
# UPDATE / DELETE: Mahalla
# =========================

@bp.post("/mahalla/update/<int:mahalla_id>")
@login_required
def mahalla_update(mahalla_id: int):
    require_role(current_user, "admin")
    m = Mahalla.query.get_or_404(mahalla_id)
    name = request.form.get("name", "").strip()
    district_id = request.form.get("district_id", type=int)

    if not name or not district_id:
        flash("Заполните название и район", "error")
        return redirect(url_for("admin.structure"))

    m.name = name
    m.district_id = district_id
    db.session.commit()
    flash("Махалля обновлена", "ok")
    return redirect(url_for("admin.structure"))

@bp.post("/mahalla/delete/<int:mahalla_id>")
@login_required
def mahalla_delete(mahalla_id: int):
    require_role(current_user, "admin")
    m = Mahalla.query.get_or_404(mahalla_id)

    # если есть улицы — нельзя
    if Street.query.filter_by(mahalla_id=m.id).count() > 0:
        flash("Нельзя удалить махаллю: в ней есть улицы", "error")
        return redirect(url_for("admin.structure"))

    db.session.delete(m)
    db.session.commit()
    flash("Махалля удалена", "ok")
    return redirect(url_for("admin.structure"))


# =========================
# UPDATE / DELETE: Street
# =========================

@bp.post("/street/update/<int:street_id>")
@login_required
def street_update(street_id: int):
    require_role(current_user, "admin")
    s = Street.query.get_or_404(street_id)
    name = request.form.get("name", "").strip()
    mahalla_id = request.form.get("mahalla_id", type=int)

    if not name or not mahalla_id:
        flash("Заполните название и махаллю", "error")
        return redirect(url_for("admin.structure"))

    s.name = name
    s.mahalla_id = mahalla_id
    db.session.commit()
    flash("Улица обновлена", "ok")
    return redirect(url_for("admin.structure"))

@bp.post("/street/delete/<int:street_id>")
@login_required
def street_delete(street_id: int):
    require_role(current_user, "admin")
    s = Street.query.get_or_404(street_id)

    # если улица назначена работнику — нельзя
    if WorkerAssignment.query.filter_by(street_id=s.id).first():
        flash("Нельзя удалить улицу: на ней уже есть работник", "error")
        return redirect(url_for("admin.structure"))

    db.session.delete(s)
    db.session.commit()
    flash("Улица удалена", "ok")
    return redirect(url_for("admin.structure"))
